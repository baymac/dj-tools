"""Production analysis pipeline (Phase 2): drive DJ Studio's bundled SDK
headlessly and write results directly to `enriched_tracks_analysis` in our DB.

Replaces the import-to-studio + enrich-studio two-step. No DJ Studio
filesystem writes — DJ Studio's audio-library-table / track-structures-table
/ compressedAudioView files are never touched. The SDK is only used as the
analysis engine (Demucs + ai-beatgrid + MIK + cf.dj.studio classifier),
exactly the way DJ Studio's UI uses it internally.

Requires DJ Studio to be QUIT (port 61894 + `.beatport/` cache locks).

Skip rules (override with --force):
- track already has a row in enriched_tracks_analysis
- track length < MIN_DURATION_MS (Demucs/beatgrid can't work reliably)
- track previously hit MAX_FAILURE_ATTEMPTS (override with --retry-failed)
"""
from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from caffeinate import caffeinate
from detect import db as detect_db
from detect.studio_sdk import (
    MAX_FAILURE_ATTEMPTS,
    MIK_CAMELOT_INT_TO_STR,
    MIN_DURATION_MS,
    SdkHelper,
    _clear_failure,
    _get_dj_studio_access_token,
    _load_failures,
    _record_failure,
    _save_failures,
    _shape_result,
    is_dj_studio_running,
)

console = Console()


def _shaped_to_analysis_fields(shaped: dict) -> dict:
    """Map a `_shape_result` dict to the `_ANALYSIS_COLS` expected by upsert_analysis."""
    rich = shaped.get("rich") or {}
    return {
        "mik_key":             MIK_CAMELOT_INT_TO_STR.get(shaped["mik_key_int"]),
        "mik_nrg":             float(shaped["mik_nrg_int"]),
        "mik_key_secondary":   rich.get("mik_key_secondary"),
        "mik_key_confidence":  rich.get("mik_key_confidence"),
        "tempo_precise":       rich.get("tempo_precise"),
        "duration_sec":        shaped["duration_sec"],
        "cue_points_count":    rich.get("cue_points_count"),
        "vocals_avg":          rich.get("vocals_avg"),
        "drums_avg":           rich.get("drums_avg"),
        "bass_avg":            rich.get("bass_avg"),
        "melody_avg":          rich.get("melody_avg"),
        "vocals_peak":         rich.get("vocals_peak"),
        "drums_peak":          rich.get("drums_peak"),
        "bass_peak":           rich.get("bass_peak"),
        "melody_peak":         rich.get("melody_peak"),
        "analysis_json":       shaped["analysis_json"],
    }


def run_studio_analyse(
    *,
    ids: Optional[list[int]] = None,
    limit: int = 0,
    verbose: bool = False,
    force: bool = False,
    retry_failed: bool = False,
) -> None:
    from paths import command_logger
    with command_logger("studio-analyse", console) as log_path, caffeinate():
        console.print(f"[dim]Log: {log_path}[/dim]")
        _run_studio_analyse_impl(
            ids=ids, limit=limit, verbose=verbose, force=force, retry_failed=retry_failed,
        )


def _run_studio_analyse_impl(
    *, ids: Optional[list[int]], limit: int, verbose: bool, force: bool, retry_failed: bool,
) -> None:
    if is_dj_studio_running():
        console.print(
            "[red]DJ Studio is currently running.[/red]\n"
            "Quit DJ.Studio (Cmd+Q) before running this command — its SDK conflicts "
            "with our pipeline (port 61894 + cache file locks)."
        )
        return

    console.print("[bold]studio-analyse[/bold]  (analysis → enriched_tracks_analysis, no DJ Studio writes)")

    candidates = detect_db.get_studio_analyse_pending(force=force)
    already_analysed = detect_db.existing_analysis_beatport_ids()
    failures = {} if retry_failed else _load_failures()
    if retry_failed:
        console.print("[dim]--retry-failed: ignoring hard-failure sidecar this run[/dim]")

    # --ids narrows the candidate set + bypasses the dedupe/short/failure
    # filters (caller is asking explicitly for these tracks). Still respects
    # `force=False` for "skip if already analysed" — pass --force to override.
    if ids:
        id_set = set(ids)
        missing = id_set - {r["beatport_id"] for r in candidates}
        for bid in missing:
            console.print(f"[yellow]bp:{bid} not in enriched_tracks — skipped[/yellow]")
        candidates = [r for r in candidates if r["beatport_id"] in id_set]

    skipped_done = skipped_short = skipped_too_many_failures = 0
    rows: list[dict] = []
    for r in candidates:
        bid = r["beatport_id"]
        if not force:
            if bid in already_analysed:
                skipped_done += 1
                continue
            length_ms = r["length_ms"] or 0
            if 0 < length_ms < MIN_DURATION_MS:
                skipped_short += 1
                continue
            entry = failures.get(bid)
            if entry and entry.get("attempts", 0) >= MAX_FAILURE_ATTEMPTS:
                skipped_too_many_failures += 1
                continue
        rows.append(dict(r))

    if limit and not ids:
        rows = rows[:limit]
    if not rows:
        console.print(
            "Nothing to analyse — every enriched track already has a row in "
            "enriched_tracks_analysis (or is below the duration / failure-attempt thresholds).\n"
            "[dim]Use --force to re-process all tracks; "
            "delete ~/Music/dj-tools/state/studio_analyse_failures.json to retry hard-failed tracks.[/dim]"
        )
        return
    console.print(
        f"{len(rows)} tracks queued{' [yellow](forced re-run)[/yellow]' if force else ''}.  "
        f"[dim]skipped: {skipped_done} already analysed, "
        f"{skipped_short} short (<30s), "
        f"{skipped_too_many_failures} hard-failed ≥{MAX_FAILURE_ATTEMPTS}× before[/dim]"
    )

    try:
        access_jwt = _get_dj_studio_access_token()
    except Exception as e:
        console.print(f"[red]Failed to get DJ Studio access token: {e}[/red]")
        console.print(
            "[yellow]Open DJ Studio briefly to refresh its session, then quit and re-run.[/yellow]"
        )
        return

    counts = {"seen": 0, "ok": 0, "fail": 0, "retried": 0, "pre_release_skipped": 0}
    failed_rows: list[dict] = []

    import datetime as _dt
    _today_iso = _dt.date.today().isoformat()

    def _is_auth_failure(err: str) -> bool:
        return "status=401" in err or "Signature is invalid" in err

    def _is_audio_unavailable(err: str) -> bool:
        # Beatport SDK signal when audio fetch is refused — typically delisted,
        # region-locked, or pre-release.
        return "Unable to get audio information" in err

    def _is_pre_release(row) -> bool:
        rd = row.get("release_date") or ""
        return len(rd) >= 10 and rd[:10] > _today_iso

    def _refresh_jwt_and_retry(row, helper, *, attempt_label: str) -> tuple[bool, str]:
        nonlocal access_jwt
        try:
            access_jwt = _get_dj_studio_access_token()
        except Exception as e:
            return False, f"jwt refresh failed: {type(e).__name__}: {e}"
        helper.set_access_jwt(access_jwt)
        progress.log(
            f"[dim]Access JWT refreshed mid-run, retrying bp:{row['beatport_id']}…[/dim]"
        )
        return _process_one(row, helper, attempt_label=attempt_label)

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=console,
    )

    def _process_one(row, helper, *, attempt_label: str = "") -> tuple[bool, str]:
        bid = row["beatport_id"]
        res = helper.analyze(bid)
        if not res["ok"]:
            return False, res.get("message", "unknown") or "unknown"

        shaped = _shape_result(bid, res["result"])
        if shaped is None:
            srv = (res.get("result") or {}).get("server") or {}
            return False, f"classifier ok={srv.get('ok')} status={srv.get('status')} body={str(srv.get('body'))[:120]}"

        # Diagnostics: log which subsystems came back empty (cf. partial-data rule).
        partials = []
        if not shaped.get("beat_data") and not shaped.get("energy_level_segments"):
            partials.append("beats+energy")
        for stem_name, key in (("vocals","vocals"),("drums","drums"),("bass","bass"),("melody","other")):
            if (shaped.get("rich") or {}).get(f"{stem_name}_avg") is None:
                partials.append(stem_name)
        if partials and verbose:
            progress.log(f"[yellow]bp:{bid} partial — missing {','.join(partials)} (saved anyway)[/yellow]")

        fields = _shaped_to_analysis_fields(shaped)
        detect_db.upsert_analysis(bid, fields)

        if verbose:
            t = res["result"].get("timing_ms", {})
            progress.log(
                f"[green]bp:{bid}[/green]{attempt_label}  "
                f"key={fields['mik_key']}  nrg={fields['mik_nrg']:.0f}  "
                f"bpm={fields['tempo_precise']:.2f}  "
                f"dur={fields['duration_sec']:.0f}s  "
                f"({t.get('total', 0)/1000:.1f}s)"
            )
        return True, ""

    with SdkHelper(access_jwt, verbose=verbose) as helper, progress:
        task = progress.add_task("Analysing…", total=len(rows))
        for row in rows:
            counts["seen"] += 1
            artist = row["artist"] or ""
            title = row["title"] or ""
            progress.update(task, advance=1, description=f"{artist} — {title}")

            ok, err = _process_one(row, helper)
            if not ok and _is_auth_failure(err):
                ok, err = _refresh_jwt_and_retry(row, helper, attempt_label=" [post-refresh]")
                if not ok and _is_auth_failure(err):
                    progress.stop()
                    raise RuntimeError(
                        f"cf.dj.studio still rejecting our JWT after a fresh refresh. "
                        f"Wrote {counts['ok']}/{counts['seen']} tracks before the failure.\n\n"
                        f"Open DJ Studio, sign back in, quit (Cmd+Q), and re-run."
                    )
            if ok:
                counts["ok"] += 1
                _clear_failure(failures, row["beatport_id"])
            elif _is_audio_unavailable(err) and _is_pre_release(row):
                # Pre-release: Beatport withholds audio until release_date. Skip
                # without bumping the failure counter so the next run after the
                # release date will retry naturally.
                counts["pre_release_skipped"] += 1
                progress.log(
                    f"[dim]bp:{row['beatport_id']} skip — pre-release "
                    f"({row['release_date']}, drops in the future)[/dim]"
                )
            else:
                failed_rows.append({"row": row, "error": err})
                if verbose:
                    progress.log(f"[yellow]bp:{row['beatport_id']} first-pass failed:[/yellow] {err[:160]}")

        if failed_rows:
            console.print(f"[dim]Retrying {len(failed_rows)} failed track(s) after 5s pause…[/dim]")
            import time as _t
            _t.sleep(5)
            retry_task = progress.add_task("Retrying…", total=len(failed_rows))
            still_failed: list[dict] = []
            for entry in failed_rows:
                row = entry["row"]
                progress.update(retry_task, advance=1,
                                description=f"{row['artist']} — {row['title']} (retry)")
                ok, err = _process_one(row, helper, attempt_label=" [retry]")
                if not ok and _is_auth_failure(err):
                    ok, err = _refresh_jwt_and_retry(row, helper, attempt_label=" [retry post-refresh]")
                    if not ok and _is_auth_failure(err):
                        progress.stop()
                        raise RuntimeError(
                            f"cf.dj.studio still rejecting our JWT after a fresh refresh during retry pass. "
                            f"Wrote {counts['ok']}/{counts['seen']} tracks before the failure."
                        )
                if ok:
                    counts["ok"] += 1
                    counts["retried"] += 1
                    _clear_failure(failures, row["beatport_id"])
                else:
                    counts["fail"] += 1
                    still_failed.append({"row": row, "error": err})
                    _record_failure(failures, row["beatport_id"], err)
                    if verbose:
                        progress.log(f"[red]bp:{row['beatport_id']} retry also failed:[/red] {err[:160]}")
            failed_rows = still_failed

    try:
        _save_failures(failures)
    except Exception as e:
        console.print(f"[yellow]Could not persist failure sidecar:[/yellow] {e}")

    console.print()
    summary = f"{counts['ok']}/{counts['seen']} written"
    if counts["retried"]:
        summary += f"  ([green]{counts['retried']} recovered on retry[/green])"
    if counts["pre_release_skipped"]:
        summary += f"  ([dim]{counts['pre_release_skipped']} pre-release skipped[/dim])"
    if counts["fail"]:
        summary += f"  ([red]{counts['fail']} failed[/red])"
    console.print(f"[bold]Done.[/bold] {summary}")
    if failed_rows:
        console.print("[red]Permanently failed tracks (this run):[/red]")
        for fr in failed_rows:
            r = fr["row"]
            attempts = failures.get(r["beatport_id"], {}).get("attempts", 1)
            console.print(f"  bp:{r['beatport_id']} (attempt {attempts}/{MAX_FAILURE_ATTEMPTS}) — {r['artist']} — {r['title']}: {fr['error'][:160]}")
