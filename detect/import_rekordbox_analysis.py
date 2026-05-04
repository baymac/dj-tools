"""Phase 2 of the rekordbox round-trip: read ANLZ files for tracks already
analyzed in rekordbox, extract data DJ Studio doesn't produce, and write a
JSON blob into `enriched_tracks_test.rk_analysis_json` (or the production
table if asked).

Pipeline order:
  1. `dj detect import-to-studio` → DJ Studio analysis (key/energy/cues/stems)
  2. `dj detect export-to-rekordbox` → push tracks to a rekordbox playlist
  3. *user opens rekordbox, runs Track → Analyze on the playlist*
  4. `dj detect import-rekordbox-analysis` ← THIS COMMAND, reads ANLZ data

What we extract from rekordbox (none of which DJ Studio produces):
  - Phrase structure (PSSI tag): mood + ordered list of phrases with
    semantic types — Intro / Verse / Bridge / Chorus / Outro for Mood 1-2,
    Intro / Up / Down / Chorus / Outro for Mood 3 (EDM).
  - Memory cues + hot cues (PCO2/PCOB tag): rekordbox-auto-placed cue
    points with names + colors, often more numerous than MIK's 8.
  - Mood classification (Low / Mid / High).

Idempotent: skip rule is `rekordbox_export_at IS NOT NULL AND
rekordbox_analysis_at IS NULL`. --force overrides.

Constraint: rekordbox MUST be quit (locks master.db). Pre-flight check
aborts with a clear message.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from detect import db as detect_db
from detect.export_to_rekordbox import is_rekordbox_running

console = Console()


# ── Mood + phrase-type mappings (from rekordcrate community RE) ───────────────
# Rekordbox classifies each track into one of three "moods" and uses different
# label vocabularies per mood. The PSSI tag stores the mood once + a kind
# integer per phrase entry.
MOOD_NAMES = {1: "Low", 2: "Mid", 3: "High (EDM)"}

# kind → label by mood
PHRASE_LABELS_LOW_MID = {
    1: "Intro", 2: "Verse 1", 3: "Verse 2", 4: "Verse 3", 5: "Verse 4",
    6: "Verse 5", 7: "Verse 6", 8: "Bridge", 9: "Chorus", 10: "Outro",
}
PHRASE_LABELS_HIGH = {
    1: "Intro", 2: "Up", 3: "Down", 4: "Chorus", 5: "Outro",
    6: "Verse 1", 7: "Verse 2",
}

def _phrase_label(mood: int, kind: int) -> str:
    if mood == 3:
        return PHRASE_LABELS_HIGH.get(kind, f"kind:{kind}")
    return PHRASE_LABELS_LOW_MID.get(kind, f"kind:{kind}")


# ── ANLZ reading ──────────────────────────────────────────────────────────────

def _resolve_anlz_dir(analysis_data_path: str, share_dir: Path) -> Optional[Path]:
    """rekordbox stores AnalysisDataPath relative to the share dir, e.g.
    `/PIONEER/USBANLZ/<x>/<yz>/<uuid>/ANLZ0000.DAT`. We strip the leading slash
    and resolve relative to share/, then return the directory containing the
    ANLZ files.
    """
    if not analysis_data_path:
        return None
    rel = analysis_data_path.lstrip("/")
    full = share_dir / rel
    # If the path points at a specific .DAT/.EXT file, take its parent.
    if full.is_file():
        return full.parent
    if full.is_dir():
        return full
    return None


def _extract_phrases(anlz, beat_to_time_sec) -> list[dict]:
    """Parse the PSSI tag if present. Returns a list of phrase dicts."""
    pssi = anlz.get_tag("PSSI") if "PSSI" in anlz.tag_types() else None
    if pssi is None:
        return []
    content = pssi.content
    mood = int(content.get("mood", 0))
    end_beat = int(content.get("end_beat", 0))
    entries = content.get("entries", [])
    out = []
    for i, e in enumerate(entries):
        beat = int(e.get("beat", 0))
        kind = int(e.get("kind", 0))
        next_beat = (
            int(entries[i + 1]["beat"]) if i + 1 < len(entries) else end_beat
        )
        out.append({
            "index": int(e.get("index", i)),
            "kind_id": kind,
            "label": _phrase_label(mood, kind),
            "start_beat": beat,
            "end_beat": next_beat,
            "length_beats": max(0, next_beat - beat),
            "start_sec": round(beat_to_time_sec(beat), 3),
            "end_sec":   round(beat_to_time_sec(next_beat), 3),
        })
    return out


def _extract_pssi_mood(anlz) -> Optional[int]:
    if "PSSI" not in anlz.tag_types():
        return None
    return int(anlz.get_tag("PSSI").content.get("mood", 0))


def _extract_cues(anlz, beat_to_time_sec) -> tuple[list[dict], list[dict]]:
    """Memory cues + hot cues from PCO2 (preferred) or PCOB (legacy).

    Returns (memory_cues, hot_cues). Each entry: {time_sec, beat_estimate,
    name, color_id, loop_time_sec, type_id}.
    """
    memory: list[dict] = []
    hot: list[dict] = []
    # Prefer PCO2 (extended, has color + name); fall back to PCOB.
    types = set(anlz.tag_types())
    for tag_name in ("PCO2", "PCOB"):
        if tag_name not in types:
            continue
        tag = anlz.get_tag(tag_name)
        for e in tag.content.get("entries", []) or []:
            time_ms = int(e.get("time", 0)) if e.get("time") not in (None, 0xFFFFFFFF) else 0
            time_sec = time_ms / 1000.0 if time_ms else 0.0
            loop_ms = int(e.get("loop_time") or 0)
            entry = {
                "time_sec": round(time_sec, 3),
                "loop_time_sec": round(loop_ms / 1000.0, 3) if loop_ms and loop_ms != 0xFFFFFFFF else 0.0,
                "type_id": int(e.get("type", 0)),
                "name": (e.get("comment") or "").strip(),
                "color_id": int(e.get("color_id", 0) or 0),
            }
            # PCO2 has hot_cue field; PCOB doesn't (cue_type at struct level).
            is_hot = bool(e.get("hot_cue", 0))
            if tag_name == "PCOB":
                # PCOB cue_type lives on the wrapper struct, not entries.
                ctype = tag.content.get("cue_type", 0)
                # Empirical: cue_type 1 = memory cues, 2 = hot cues
                is_hot = ctype == 2
            (hot if is_hot else memory).append(entry)
        # Use the first tag we find — don't double-count.
        break
    return memory, hot


def _build_rk_blob(rb_content, anlz_files: list, bpm_x100: int) -> dict:
    """Stitch together what we found across all ANLZ files for the track."""
    # bpm to beat→sec converter (assumes near-constant tempo; for variable
    # tempo the real per-beat time would come from PQTZ but we keep it simple).
    bpm = (bpm_x100 / 100.0) if bpm_x100 else 0
    def beat_to_sec(beat: int) -> float:
        if not bpm or not beat:
            return 0.0
        return (beat - 1) * 60.0 / bpm if beat > 0 else 0.0

    phrases: list[dict] = []
    memory_cues: list[dict] = []
    hot_cues: list[dict] = []
    mood: Optional[int] = None
    seen_tags: set[str] = set()

    for anlz in anlz_files:
        seen_tags.update(anlz.tag_types())
        if mood is None:
            mood = _extract_pssi_mood(anlz)
        if not phrases:
            phrases = _extract_phrases(anlz, beat_to_sec)
        if not memory_cues and not hot_cues:
            mc, hc = _extract_cues(anlz, beat_to_sec)
            memory_cues, hot_cues = mc, hc

    return {
        "version": 1,
        "rekordbox_track_id": str(rb_content.ID),
        "mood_id": mood,
        "mood_name": MOOD_NAMES.get(mood) if mood is not None else None,
        "phrases": phrases,
        "memory_cues": memory_cues,
        "hot_cues": hot_cues,
        "rekordbox_bpm": bpm or None,
        "tags_seen": sorted(seen_tags),
    }


# ── Top-level runner ──────────────────────────────────────────────────────────

def run_import_rekordbox_analysis(
    *,
    table: str = "enriched_tracks_test",
    limit: int = 0,
    force: bool = False,
    verbose: bool = False,
) -> None:
    if is_rekordbox_running():
        console.print(
            "[red]rekordbox is currently running.[/red]\n"
            "Quit rekordbox before running this command — it locks master.db while open."
        )
        return

    detect_db.migrate()
    pending = detect_db.get_rekordbox_analysis_pending(table=table, force=force)
    if limit:
        pending = pending[:limit]
    if not pending:
        console.print(
            f"Nothing to ingest. Either no tracks have rekordbox_export_at set yet "
            f"(run [cyan]export-to-rekordbox[/cyan] first), or all exported tracks have "
            f"already been ingested."
        )
        return

    console.print(
        f"[bold]import-rekordbox-analysis[/bold] ← {len(pending)} tracks from "
        f"[cyan]{table}[/cyan]{' [yellow](forced)[/yellow]' if force else ''}"
    )

    # Lazy imports
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.anlz import AnlzFile
    from rekordbox.constants import RB_SHARE

    db = Rekordbox6Database()

    counts = {"seen": 0, "missing_track": 0, "missing_anlz": 0,
              "no_pssi": 0, "ok": 0}

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(),
        console=console,
    )

    try:
        with progress:
            t = progress.add_task("Ingesting…", total=len(pending))
            for row in pending:
                counts["seen"] += 1
                bid = row["beatport_id"]
                progress.update(t, advance=1, description=f"{row['artist']} — {row['title']}"[:60])

                folder_path = f"/v4/catalog/tracks/{bid}/"
                content = db.get_content(FolderPath=folder_path).first()
                if content is None:
                    counts["missing_track"] += 1
                    if verbose:
                        progress.log(f"[yellow]bp:{bid} not in rekordbox library[/yellow]")
                    continue

                anlz_dir = _resolve_anlz_dir(content.AnalysisDataPath, RB_SHARE)
                if anlz_dir is None or not anlz_dir.is_dir():
                    counts["missing_anlz"] += 1
                    if verbose:
                        progress.log(f"[yellow]bp:{bid} no ANLZ dir at {content.AnalysisDataPath}[/yellow]")
                    continue

                # Read both .DAT and .EXT (different rekordbox versions split tags).
                anlz_files = []
                for fp in sorted(anlz_dir.iterdir()):
                    if fp.suffix.upper() in (".DAT", ".EXT") and fp.is_file():
                        try:
                            anlz_files.append(AnlzFile.parse_file(str(fp)))
                        except Exception as e:
                            if verbose:
                                progress.log(f"[dim]bp:{bid} parse {fp.name}: {e}[/dim]")

                if not anlz_files:
                    counts["missing_anlz"] += 1
                    continue

                blob = _build_rk_blob(content, anlz_files, content.BPM)

                if not blob.get("phrases"):
                    counts["no_pssi"] += 1
                    if verbose:
                        progress.log(
                            f"[yellow]bp:{bid} ANLZ found but no PSSI — track probably "
                            f"hasn't been Analyzed in rekordbox yet[/yellow]"
                        )
                    # Still write the blob (cues + tags-seen are useful) but DON'T
                    # mark complete, so a later run after analysis re-tries.
                    detect_db.update_rk_analysis_json(table, bid, json.dumps(blob, separators=(",", ":")))
                    continue

                detect_db.update_rk_analysis_json(table, bid, json.dumps(blob, separators=(",", ":")))
                detect_db.mark_pipeline_done(table, bid, "rekordbox_analysis_at")
                counts["ok"] += 1

                if verbose:
                    progress.log(
                        f"[green]bp:{bid}[/green] mood={blob['mood_name']}  "
                        f"phrases={len(blob['phrases'])}  "
                        f"mem={len(blob['memory_cues'])}  hot={len(blob['hot_cues'])}"
                    )

        console.print()
        console.print(f"[bold]Done.[/bold]")
        console.print(f"  ingested:           {counts['ok']}")
        console.print(f"  no PSSI yet:        {counts['no_pssi']}  [dim](not yet analyzed in rekordbox)[/dim]")
        console.print(f"  missing rb track:   {counts['missing_track']}")
        console.print(f"  missing ANLZ files: {counts['missing_anlz']}")
    finally:
        db.close()
