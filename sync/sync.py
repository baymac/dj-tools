"""Sync loop: Apple Music tracks → Beatport genre playlists."""
from __future__ import annotations

import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor
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

from connections import beatport as api, matching, musickit
from sync import classifier, db

console = Console()


def _bp_url(match: dict) -> str:
    slug = match.get("slug", "")
    track_id = match.get("id", "")
    if slug and track_id:
        return f"https://www.beatport.com/track/{slug}/{track_id}"
    return ""


LIBRARY_KEY = "__library__"
FAVORITES_KEY = "__favorites__"
LIB_AND_FAV_KEY = "__library_and_fav__"
ALL_KEY = "__all__"

_REPO_ROOT = Path(__file__).resolve().parent.parent
from paths import LOGS_DIR as _LOGS_ROOT
_LOG_DIR = _LOGS_ROOT / "sync-music-beatport"
_BEATPORT_CSV = _REPO_ROOT / ".context" / "attachments" / "My Beatport Library.csv"


def get_or_refresh_token() -> str:
    """Return a Beatport Bearer token.

    Priority:
    1. BEATPORT_ACCESS_TOKEN env var
    2. Cached token in DB
    3. Refresh via BEATPORT_SESSION_TOKEN cookie
    4. Browser login via BEATPORT_USERNAME + BEATPORT_PASSWORD
    """
    import time as _time
    session_cookie = os.environ.get("BEATPORT_SESSION_TOKEN", "").strip()

    access_token = os.environ.get("BEATPORT_ACCESS_TOKEN", "").strip()
    if access_token:
        if not access_token.startswith("Bearer "):
            access_token = f"Bearer {access_token}"
        payload = api._jwt_payload(access_token)
        if payload.get("exp", 0) > _time.time():
            return access_token
        # expired — fall through to refresh

    if session_cookie:
        new_token = api.refresh_via_session(session_cookie)
        if new_token:
            api.save_token_to_env(new_token)
            return new_token

    username = os.environ.get("BEATPORT_USERNAME", "").strip()
    password = os.environ.get("BEATPORT_PASSWORD", "").strip() or None

    console.print("[yellow]Session expired — trying browser login (headless)…[/yellow]")
    try:
        token, session = api.capture_token(username or None, password, headless=True)
        api.save_token_to_env(token, session)
        return token
    except Exception:
        pass

    console.print("[yellow]Headless login failed — opening browser window…[/yellow]")
    try:
        token, session = api.capture_token(username or None, password, headless=False)
        api.save_token_to_env(token, session)
        return token
    except Exception:
        pass

    console.print(
        "[red]Session expired and browser login failed.[/red]\n"
        "Run [bold]dj login-beatport --ui[/bold] to log in interactively."
    )
    sys.exit(1)


def make_bp_client() -> tuple[api.Beatport, object]:
    token = get_or_refresh_token()
    client = api.make_client(token)

    def on_401():
        nonlocal token
        console.print("[yellow]Token expired — refreshing via session…[/yellow]")
        import os as _os
        session_cookie = _os.environ.get("BEATPORT_SESSION_TOKEN", "").strip()
        if session_cookie:
            new_token = api.refresh_via_session(session_cookie)
            if new_token:
                token = new_token
                client.headers["authorization"] = token
                api.save_token_to_env(token)
                console.print("[dim]Token refreshed.[/dim]")
                return
        console.print(
            "[red]Token expired and session refresh failed.[/red]\n\n"
            "The session cookie's refresh token has been rotated. Get fresh tokens:\n"
            "  1. Open beatport.com in a browser (logged in)\n"
            "  2. DevTools → Network → /api/auth/session → copy token.accessToken\n"
            "     → set as [bold]BEATPORT_ACCESS_TOKEN[/bold] in .env\n"
            "  3. DevTools → Application → Cookies → copy [bold]__Secure-next-auth.session-token[/bold]\n"
            "     → set as [bold]BEATPORT_SESSION_TOKEN[/bold] in .env"
        )
        sys.exit(1)

    beatport = api.Beatport(client=client, on_401=on_401)
    return beatport, client


def resolve_destinations(
    beatport: api.Beatport,
    dry_run: bool,
    single_playlist_name: Optional[str] = None,
) -> dict[str, int]:
    """Return dest_name → playlist_id. Auto-creates missing playlists.

    If single_playlist_name is given, resolve only that one (used in playlist
    mirror mode, where no genre classification is applied).
    """
    playlists = beatport.list_my_playlists()
    name_to_id = {pl["name"]: pl["id"] for pl in playlists}
    dest_map: dict[str, int] = {}

    id_to_name = {pl["id"]: pl["name"] for pl in playlists}
    targets = [single_playlist_name] if single_playlist_name else sorted(classifier.DESTINATION_PLAYLISTS)

    for name in targets:
        if name in name_to_id:
            dest_map[name] = name_to_id[name]
        elif name.lstrip("-").isdigit() and int(name) in id_to_name:
            dest_map[name] = int(name)
        elif not dry_run:
            try:
                result = beatport.create_playlist(name)
                pl_id = result.get("id")
                if pl_id:
                    dest_map[name] = pl_id
                    console.print(f"  [green]Created[/green] playlist: {name}")
                else:
                    console.print(f"  [yellow]Warning:[/yellow] Could not create playlist '{name}'.")
            except Exception as e:
                console.print(f"  [yellow]Warning:[/yellow] Could not create playlist '{name}': {e}")
        else:
            console.print(f"  [dim]Would create playlist: {name}[/dim]")

    return dest_map


def _load_dest_track_ids(beatport: api.Beatport, dest_map: dict[str, int]) -> dict[str, set[int]]:
    """Per-destination existing track-id sets for dedup. Prefers a local CSV (instant)."""
    dest_track_ids: dict[str, set[int]] = {}
    if _BEATPORT_CSV.exists():
        with _BEATPORT_CSV.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                pl = row["Playlist name"].strip()
                tid = row["beatport - id"].strip()
                if tid.isdigit():
                    dest_track_ids.setdefault(pl, set()).add(int(tid))
        total = sum(len(v) for v in dest_track_ids.values())
        console.print(f"[dim]Loaded existing track IDs from CSV ({total} tracks across {len(dest_track_ids)} playlists)[/dim]")
        return dest_track_ids

    token = beatport.client.headers.get("authorization", "")

    def _fetch_ids(name_pl: tuple[str, int]) -> tuple[str, set[int]]:
        name, pl_id = name_pl
        local_bp = api.Beatport(api.make_client(token))
        return name, local_bp.list_track_ids(pl_id)

    console.print("Fetching existing track IDs from Beatport API…")
    with ThreadPoolExecutor(max_workers=min(len(dest_map), 10)) as pool:
        for name, ids in pool.map(_fetch_ids, dest_map.items()):
            dest_track_ids[name] = ids
    return dest_track_ids


def _resolve_source(
    playlist: Optional[str],
    use_library: bool,
    use_favorites: bool,
    use_lib_and_fav: bool,
    use_all: bool,
) -> tuple[str, str]:
    """Return (source_key, display_label)."""
    if use_library:
        return LIBRARY_KEY, "Apple Music library songs"
    if use_favorites:
        return FAVORITES_KEY, "Favourite Songs"
    if use_lib_and_fav:
        return LIB_AND_FAV_KEY, "library + Favourite Songs"
    if use_all:
        return ALL_KEY, "all Apple Music songs"

    if not playlist:
        try:
            names = musickit.list_playlists()
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)
        console.print("Available Apple Music playlists:")
        for i, name in enumerate(sorted(names), 1):
            console.print(f"  {i:3}. {name}")
        playlist = input("Playlist name: ")
    return playlist, playlist


def _track_iterator(
    use_library: bool, use_favorites: bool, use_lib_and_fav: bool, use_all: bool, source_key: str,
):
    if use_library:
        return "library songs", musickit.stream_library_tracks()
    if use_favorites:
        return "Favourite Songs", musickit.stream_favorite_tracks()
    if use_lib_and_fav:
        return "library + Favourite Songs", musickit.stream_library_and_favorites_tracks()
    if use_all:
        return "all songs", musickit.stream_all_tracks()
    return f"'{source_key}'", musickit.stream_playlist_tracks(source_key)


def run_sync(
    playlist: Optional[str],
    use_library: bool,
    use_favorites: bool,
    use_lib_and_fav: bool,
    use_all: bool,
    dry_run: bool,
    limit: int,
    verbose: bool,
    threshold: float,
) -> None:
    source_key, display_label = _resolve_source(
        playlist, use_library, use_favorites, use_lib_and_fav, use_all,
    )

    # Playlist-mirror mode: a named Apple Music playlist syncs to a same-named
    # Beatport playlist with no genre classification.
    is_playlist_mode = source_key not in (LIBRARY_KEY, FAVORITES_KEY, LIB_AND_FAV_KEY, ALL_KEY)

    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] — no changes will be made")

    if is_playlist_mode:
        console.print(f"Syncing [bold]{display_label}[/bold] → Beatport playlist [bold]{source_key}[/bold]")
    else:
        console.print(f"Syncing [bold]{display_label}[/bold] → Beatport genre playlists")

    run_id = db.start_sync_run(source_key)
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

    def _log(plain: str, rich: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log_file.write(f"{ts}  {plain}\n")
        log_file.flush()
        if verbose:
            progress.log(rich or plain)

    beatport, http_client = make_bp_client()

    synced_set = db.load_synced_set(source_key)
    console.print(f"[dim]{len(synced_set)} tracks already synced for {display_label}[/dim]")

    console.print("Resolving destination playlists on Beatport…")
    dest_map = resolve_destinations(
        beatport,
        dry_run=dry_run,
        single_playlist_name=source_key if is_playlist_mode else None,
    )

    dest_track_ids: dict[str, set[int]] = {}
    if not dry_run:
        if is_playlist_mode:
            # One playlist to dedup against — fetch its track IDs directly.
            for name, pl_id in dest_map.items():
                dest_track_ids[name] = beatport.list_track_ids(pl_id)
        else:
            dest_track_ids = _load_dest_track_ids(beatport, dest_map)

    _stream_label, track_iterator = _track_iterator(
        use_library, use_favorites, use_lib_and_fav, use_all, source_key,
    )
    console.print(f"Loading {_stream_label} from Apple Music…")
    am_tracks: list[dict] = []
    try:
        for t in track_iterator:
            am_tracks.append(t)
            if limit and len(am_tracks) >= limit:
                break
    except RuntimeError as e:
        console.print(f"[red]MusicKit error:[/red] {e}")
        sys.exit(1)

    if use_library:
        # Sort ascending so cursor advances cleanly even on partial run.
        am_tracks.sort(key=lambda t: t.get("library_added_date") or "")
        lib_cursor = db.get_cursor(LIBRARY_KEY)
        if lib_cursor:
            before = len(am_tracks)
            am_tracks = [t for t in am_tracks if t["library_added_date"] > lib_cursor]
            console.print(
                f"[dim]Library cursor: {lib_cursor}"
                f" — skipping {before - len(am_tracks)} already-covered songs[/dim]"
            )

    total_count = len(am_tracks)
    skipped_synced = sum(1 for t in am_tracks if t.get("catalog_id") and t["catalog_id"] in synced_set)
    skipped_no_id = sum(1 for t in am_tracks if not t.get("catalog_id"))
    am_tracks = [t for t in am_tracks if t.get("catalog_id") and t["catalog_id"] not in synced_set]
    console.print(
        f"[bold]{total_count}[/bold] tracks from Apple Music"
        f" — [bold]{len(am_tracks)}[/bold] to process"
        f" ([dim]{skipped_synced} already synced, {skipped_no_id} no catalog id[/dim])"
    )

    counts = {
        "seen": 0, "added": 0, "skipped": skipped_synced, "no_match": 0,
        "no_classify": 0, "no_catalog_id": skipped_no_id, "failed": 0, "partial": False,
    }

    with progress:
        task = progress.add_task("Syncing…", total=len(am_tracks))

        for track in am_tracks:
            counts["seen"] += 1
            progress.update(task, advance=1)

            catalog_id = track.get("catalog_id", "")
            name = track.get("name", "")
            artist = track.get("artist", "")

            progress.update(task, description=f"{artist} — {name}")

            query = f"{artist} {matching.search_query(name)}"
            results = beatport.search_tracks(query, per_page=10, debug=verbose)

            if results is None:
                counts["failed"] += 1
                _log(f"search_error  {artist} — {name}",
                     f"[red]search error:[/red] {artist} — {name}")
                continue

            if not results:
                if not dry_run:
                    db.mark_synced(catalog_id, source_key, "no_search_results")
                    synced_set.add(catalog_id)
                counts["no_match"] += 1
                _log(f"no_match  {artist} — {name}",
                     f"[yellow]no beatport match:[/yellow] {artist} — {name}")
                continue

            match, score = matching.best_match(name, artist, results, threshold)
            if not match:
                if not dry_run:
                    db.mark_synced(catalog_id, source_key, "fuzzy_miss")
                    synced_set.add(catalog_id)
                counts["no_match"] += 1
                best = results[0]
                bp_artists = ", ".join(a.get("name", "") for a in best.get("artists", []))
                _log(
                    f"fuzzy_miss  {artist} — {name}  →  best: {bp_artists} — {best.get('name', '')}",
                    f"[yellow]fuzzy miss:[/yellow] {artist} — {name}"
                    f"  →  best: {bp_artists} — {best.get('name', '')} (score below threshold)",
                )
                continue

            if is_playlist_mode:
                # No genre classification — mirror match into the same-named playlist.
                dest_name = source_key
            else:
                bp_genre = (match.get("genre") or {}).get("name")
                dest_name = classifier.classify(bp_genre)
                if not dest_name:
                    if not dry_run:
                        db.mark_synced(catalog_id, source_key, "no_classify")
                        synced_set.add(catalog_id)
                    counts["no_classify"] += 1
                    _log(
                        f"no_classify  {artist} — {name}  (bp genre: {bp_genre!r})",
                        f"[dim]no genre classify:[/dim] {artist} — {name}  (bp genre: {bp_genre!r})",
                    )
                    continue

            bp_track_id = match.get("id")
            dest_id = dest_map.get(dest_name)
            if not dest_id:
                counts["failed"] += 1
                continue

            bp_url = _bp_url(match)

            if dry_run:
                bp_name = match.get("name", "")
                bp_artists = ", ".join(a.get("name", "") for a in match.get("artists", []))
                url_suffix = f"  {bp_url}" if bp_url else ""
                _log(
                    f"would_add  {artist} — {name}  →  {bp_artists} — {bp_name} → {dest_name} (score={score:.2f}){url_suffix}",
                    f"[green]would add:[/green] {artist} — {name}"
                    f"  →  {bp_artists} — {bp_name} → [bold]{dest_name}[/bold] (score={score:.2f}){url_suffix}",
                )
                counts["added"] += 1
                continue

            if bp_track_id and bp_track_id in dest_track_ids.get(dest_name, set()):
                db.mark_synced(catalog_id, source_key, "duplicate",
                               beatport_track_id=bp_track_id, dest_playlist=dest_name)
                synced_set.add(catalog_id)
                counts["skipped"] += 1
                url_suffix = f"  {bp_url}" if bp_url else ""
                _log(f"duplicate  {artist} — {name} → {dest_name}{url_suffix}",
                     f"[dim]duplicate (already in playlist):[/dim] {artist} — {name} → {dest_name}{url_suffix}")
                continue

            try:
                resp = beatport.add_track(dest_id, bp_track_id)
                items = resp.get("items") or []
                if items and bp_track_id:
                    dest_track_ids.setdefault(dest_name, set()).add(bp_track_id)
                db.mark_synced(
                    catalog_id, source_key,
                    "added" if items else "noop_empty_items",
                    beatport_track_id=bp_track_id,
                    dest_playlist=dest_name,
                )
                synced_set.add(catalog_id)
                counts["added"] += 1
                url_suffix = f"  {bp_url}" if bp_url else ""
                _log(f"added  {artist} — {name} → {dest_name}{url_suffix}",
                     f"[green]added:[/green] {artist} — {name} → [bold]{dest_name}[/bold]{url_suffix}")
            except Exception as e:
                _log(f"add_failed  {artist} — {name}: {e}",
                     f"[red]add_track failed:[/red] {artist} — {name}: {e}")
                counts["failed"] += 1

    http_client.close()

    # Advance library cursor only when there were no errors.
    if use_library and not dry_run and counts["failed"] == 0 and am_tracks:
        max_date = max((t.get("library_added_date") or "" for t in am_tracks), default="")
        if max_date:
            db.set_cursor(LIBRARY_KEY, max_date)
            console.print(f"[dim]Library cursor advanced to {max_date}[/dim]")

    db.finish_sync_run(
        run_id,
        tracks_seen=counts["seen"],
        tracks_added=counts["added"],
        tracks_skipped=counts["skipped"],
        tracks_failed=counts["failed"],
        status="done",
    )

    summary_lines = [
        f"--- sync {'(dry run) ' if dry_run else ''}complete ---",
        f"tracks_seen:       {counts['seen']}",
        f"added:             {counts['added']}",
        f"already_synced:    {counts['skipped']}",
        f"no_beatport_match: {counts['no_match']}",
        f"no_genre_classify: {counts['no_classify']}",
        f"no_catalog_id:     {counts['no_catalog_id']}",
        f"errors:            {counts['failed']}",
    ]
    for line in summary_lines:
        log_file.write(line + "\n")
    log_file.close()

    console.print()
    console.print(f"[bold]Sync {'(dry run) ' if dry_run else ''}complete[/bold]")
    console.print(f"  Tracks seen:       {counts['seen']}")
    console.print(f"  Added to Beatport: {counts['added']}")
    console.print(f"  Already synced:    {counts['skipped']}")
    console.print(f"  No Beatport match: {counts['no_match']}")
    console.print(f"  No genre classify: {counts['no_classify']}")
    console.print(f"  No catalog ID:     {counts['no_catalog_id']}")
    console.print(f"  Errors (retry):    {counts['failed']}")
    console.print(f"[dim]Log: {log_path}[/dim]")
