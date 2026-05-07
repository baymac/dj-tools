"""Enrich detected tracks with Beatport metadata (bpm, key, genre, release_date, beatport_id, beatport_link)."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
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
    TimeElapsedColumn,
)

from caffeinate import caffeinate
from connections import beatport as bp_api
from connections.matching import MATCH_THRESHOLD, best_match, search_query, strip_remix
from detect import db as detect_db
from detect.db import mark_enrich_miss

console = Console()

from paths import LOGS_DIR as _LOGS_ROOT
_LOG_DIR = _LOGS_ROOT / "enrich"


def _get_token() -> str:
    """Get a Beatport Bearer token.

    Priority:
    1. BEATPORT_ACCESS_TOKEN env var
    2. BEATPORT_SESSION_TOKEN cookie → refresh via /api/auth/session (fallback)
    3. BEATPORT_USERNAME + BEATPORT_PASSWORD → browser login (last resort)
    """
    import time as _time
    session_cookie = os.environ.get("BEATPORT_SESSION_TOKEN", "").strip()

    access_token = os.environ.get("BEATPORT_ACCESS_TOKEN", "").strip()
    if access_token:
        if not access_token.startswith("Bearer "):
            access_token = f"Bearer {access_token}"
        payload = bp_api._jwt_payload(access_token)
        if payload.get("exp", 0) > _time.time():
            return access_token
        # expired — fall through to refresh

    if session_cookie:
        new_token = bp_api.refresh_via_session(session_cookie)
        if new_token:
            bp_api.save_token_to_env(new_token)
            return new_token

    username = os.environ.get("BEATPORT_USERNAME", "").strip()
    password = os.environ.get("BEATPORT_PASSWORD", "").strip() or None

    console.print("[dim]Session expired — trying browser login (headless)…[/dim]")
    try:
        token, session = bp_api.capture_token(username or None, password, headless=True)
        bp_api.save_token_to_env(token, session)
        return token
    except Exception:
        pass

    console.print("[dim]Headless login failed — opening browser window…[/dim]")
    try:
        token, session = bp_api.capture_token(username or None, password, headless=False)
        bp_api.save_token_to_env(token, session)
        return token
    except Exception:
        pass

    console.print(
        "[red]Session expired and browser login failed.[/red]\n"
        "Run [bold]dj login-beatport --ui[/bold] to log in interactively."
    )
    sys.exit(1)


def _try_refresh() -> Optional[str]:
    """Try to refresh the access token via the NextAuth session cookie."""
    session_cookie = os.environ.get("BEATPORT_SESSION_TOKEN", "").strip()
    if not session_cookie:
        return None
    return bp_api.refresh_via_session(session_cookie)


def _bp_meta(match: dict) -> dict:
    """Extract enrichment fields from a Beatport search result."""
    slug = match.get("slug", "")
    track_id = match.get("id")
    link = f"https://www.beatport.com/track/{slug}/{track_id}" if slug and track_id else ""
    key_obj = match.get("key") or {}
    release_obj = match.get("release") or {}
    release_date = (
        match.get("publish_date")
        or match.get("new_release_date")
        or (release_obj.get("date") if isinstance(release_obj, dict) else None)
        or match.get("release_date")
    )
    return {
        "beatport_id": track_id,
        "beatport_link": link,
        "bpm": match.get("bpm"),
        "key": key_obj.get("camelot_name") or key_obj.get("name"),
        "genre": (match.get("genre") or {}).get("name"),
        "release_date": release_date,
    }




def run_enrich(
    dry_run: bool,
    limit: int,
    verbose: bool,
    threshold: float,
    retry_misses: bool,
) -> None:
    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] — no changes will be made")

    if retry_misses:
        console.print("Loading previously missed tracks for retry…")
        tracks = detect_db.get_retry_tracks()
    else:
        console.print("Loading un-enriched detected tracks…")
        tracks = detect_db.get_unenriched_tracks()
    if limit:
        tracks = tracks[:limit]

    if not tracks:
        console.print("Nothing to enrich — all detected tracks already have Beatport data.")
        return

    console.print(f"[bold]{len(tracks)}[/bold] tracks to enrich")

    token = _get_token()
    http_client = bp_api.make_client(token)

    def on_401() -> None:
        nonlocal token
        new_token = _try_refresh()
        if new_token:
            console.print("[dim]Token refreshed.[/dim]")
            token = new_token
            http_client.headers["authorization"] = token
            bp_api.save_token_to_env(token)
        else:
            raise bp_api.AuthExpiredError(
                "Beatport token expired and session refresh failed.\n"
                "The session cookie's refresh token has been rotated — get fresh tokens:\n"
                "  1. Open beatport.com (logged in)\n"
                "  2. DevTools → Network → /api/auth/session → copy token.accessToken\n"
                "     → set as BEATPORT_ACCESS_TOKEN in .env\n"
                "  3. DevTools → Application → Cookies → copy __Secure-next-auth.session-token\n"
                "     → set as BEATPORT_SESSION_TOKEN in .env"
            )

    beatport = bp_api.Beatport(client=http_client, on_401=on_401)

    run_id = detect_db.start_enrich_run()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_path = _LOG_DIR / f"{date_str}_{run_id}.log"
    log_file = log_path.open("w", encoding="utf-8")
    console.print(f"[dim]Log: {log_path}[/dim]")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    def _log(plain: str, rich_msg: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log_file.write(f"{ts}  {plain}\n")
        log_file.flush()
        if verbose:
            progress.log(rich_msg or plain)

    counts = {"seen": 0, "found": 0, "not_found": 0, "fuzzy_miss": 0, "failed": 0}

    with caffeinate(), progress:
        task = progress.add_task("Enriching…", total=len(tracks))

        for track in tracks:
            counts["seen"] += 1
            progress.update(task, advance=1)

            track_id = track["id"]
            artist = track["artist"] or ""
            title = track["title"] or ""

            progress.update(task, description=f"{artist} — {title}")

            artist_query = re.sub(r"\s*[\(\[].*?[\)\]]", "", artist).strip()
            query = f"{artist_query} {search_query(title)}"
            try:
                results = beatport.search_tracks(query, per_page=10, debug=verbose)
            except bp_api.AuthExpiredError as e:
                progress.stop()
                console.print(f"\n[red]Auth failed:[/red] {e}")
                console.print(
                    "Get fresh tokens:\n"
                    "  1. Open beatport.com in a browser (logged in)\n"
                    "  2. DevTools → Network → /api/auth/session → copy [bold]token.accessToken[/bold]\n"
                    "     → set as BEATPORT_ACCESS_TOKEN in .env\n"
                    "  3. DevTools → Application → Cookies → copy [bold]__Secure-next-auth.session-token[/bold]\n"
                    "     → set as BEATPORT_SESSION_TOKEN in .env"
                )
                http_client.close()
                sys.exit(1)

            if results is None:
                counts["failed"] += 1
                _log(f"search_error  {artist} — {title}",
                     f"[red]search error:[/red] {artist} — {title}")
                continue

            if not results:
                counts["not_found"] += 1
                _log(f"no_results  {artist} — {title}",
                     f"[yellow]no results:[/yellow] {artist} — {title}")
                if not dry_run:
                    mark_enrich_miss(track_id, "not_found")
                continue

            match, score = best_match(title, artist, results, threshold)
            if not match:
                # Retry with base title when a remix/edit/mix tag caused the mismatch
                base_title = strip_remix(title)
                if base_title:
                    base_query = f"{artist_query} {search_query(base_title)}"
                    try:
                        base_results = beatport.search_tracks(base_query, per_page=10, debug=verbose)
                    except bp_api.AuthExpiredError:
                        raise
                    except Exception:
                        base_results = None
                    if base_results:
                        match, score = best_match(base_title, artist, base_results, threshold)
                        if match and verbose:
                            progress.log(
                                f"[green]remix fallback:[/green] {artist} — {title}  "
                                f"→  matched as base title '{base_title}'  (score={score:.2f})"
                            )

            # SoundCloud uploaders sometimes use "Title - Artist (Mix)" instead of
            # the standard "Artist - Title (Mix)" — re-score the same Beatport
            # results with our artist/title swapped before giving up.
            if not match:
                m, s = best_match(artist, title, results, threshold)
                if m:
                    match, score = m, s
                    if verbose:
                        progress.log(
                            f"[green]swap fallback:[/green] {artist} — {title}  "
                            f"→  re-scored with artist/title swapped  (score={score:.2f})"
                        )

            # Title contains an internal dash (e.g. "Carson Paskill — Jackie Hollander
            # - You Go I Go (Remix)") → split and try the inner pair as artist/title.
            if not match:
                inner = strip_remix(title) or title
                m_inner = re.split(r"\s+-\s+|-\s+", inner, maxsplit=1)
                if len(m_inner) == 2 and m_inner[0].strip() and m_inner[1].strip():
                    inner_artist, inner_title = m_inner[0].strip(), m_inner[1].strip()
                    m, s = best_match(inner_title, inner_artist, results, threshold)
                    if m:
                        match, score = m, s
                        if verbose:
                            progress.log(
                                f"[green]dash-split fallback:[/green] {artist} — {title}  "
                                f"→  parsed as '{inner_artist}' — '{inner_title}'  (score={score:.2f})"
                            )

            if not match:
                counts["fuzzy_miss"] += 1
                best_r = results[0]
                bp_artists = ", ".join(a.get("name", "") for a in best_r.get("artists", []))
                _log(
                    f"fuzzy_miss  {artist} — {title}  score={score:.2f}  best: {bp_artists} — {best_r.get('name', '')}",
                    f"[yellow]fuzzy miss:[/yellow] {artist} — {title}  score={score:.2f}",
                )
                if not dry_run:
                    mark_enrich_miss(track_id, "fuzzy_miss")
                continue

            meta = _bp_meta(match)

            if dry_run:
                _log(
                    f"would_enrich  {artist} — {title}  →  bp:{meta['beatport_id']}  score={score:.2f}",
                    f"[green]would enrich:[/green] {artist} — {title}  →  {meta['beatport_link']}  (score={score:.2f})",
                )
                counts["found"] += 1
                continue

            # Fetch full Beatport catalog detail before upserting so the lean
            # row in enriched_tracks gets label/ISRC/sub_genre/etc. on the
            # initial INSERT.
            extras = {}
            try:
                full_track = beatport.get_track(meta["beatport_id"])
                if full_track:
                    label_obj = (full_track.get("release") or {}).get("label") or {}
                    sub_genre_obj = full_track.get("sub_genre") or {}
                    extras = {
                        "mix_name": full_track.get("mix_name"),
                        "label": label_obj.get("name") if isinstance(label_obj, dict) else None,
                        "catalog_number": full_track.get("catalog_number"),
                        "isrc": full_track.get("isrc"),
                        "sub_genre": sub_genre_obj.get("name") if isinstance(sub_genre_obj, dict) else None,
                        "length_ms": full_track.get("length_ms"),
                    }
            except Exception:
                pass  # Non-critical — basic enrich still succeeds with empty extras.

            detect_db.upsert_enriched(track_id, meta, extras=extras)

            counts["found"] += 1
            _log(
                f"enriched  {artist} — {title}  →  bp:{meta['beatport_id']}  score={score:.2f}",
                f"[green]enriched:[/green] {artist} — {title}  →  {meta['beatport_link']}",
            )

    http_client.close()

    if not dry_run:
        detect_db.finish_enrich_run(
            run_id,
            seen=counts["seen"],
            found=counts["found"],
            not_found=counts["not_found"],
            fuzzy_miss=counts["fuzzy_miss"],
        )

    summary = [
        f"--- enrich {'(dry run) ' if dry_run else ''}complete ---",
        f"tracks_seen:   {counts['seen']}",
        f"enriched:      {counts['found']}",
        f"no_results:    {counts['not_found']}",
        f"fuzzy_miss:    {counts['fuzzy_miss']}",
        f"search_errors: {counts['failed']}",
    ]
    for line in summary:
        log_file.write(line + "\n")
    log_file.close()

    console.print()
    console.print(f"[bold]Enrich {'(dry run) ' if dry_run else ''}complete[/bold]")
    console.print(f"  Seen:          {counts['seen']}")
    console.print(f"  Enriched:      {counts['found']}")
    console.print(f"  No results:    {counts['not_found']}")
    console.print(f"  Fuzzy miss:    {counts['fuzzy_miss']}")
    console.print(f"  Search errors: {counts['failed']}")
    console.print(f"[dim]Log: {log_path}[/dim]")
