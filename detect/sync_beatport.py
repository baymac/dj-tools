"""Sync Beatport playlists → enriched_tracks."""
from __future__ import annotations

import sys

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

from connections import beatport as bp_api
from detect import db as detect_db
from detect.enrich import _get_token, _try_refresh

console = Console()


def _track_meta(track: dict) -> dict:
    slug = track.get("slug", "")
    track_id = track.get("id")
    link = f"https://www.beatport.com/track/{slug}/{track_id}" if slug and track_id else ""
    key_obj = track.get("key") or {}
    release_obj = track.get("release") or {}
    release_date = (
        track.get("publish_date")
        or track.get("new_release_date")
        or (release_obj.get("date") if isinstance(release_obj, dict) else None)
        or track.get("release_date")
    )
    return {
        "beatport_id": track_id,
        "beatport_link": link,
        "bpm": track.get("bpm"),
        "key": key_obj.get("camelot_name") or key_obj.get("name"),
        "genre": (track.get("genre") or {}).get("name"),
        "release_date": release_date,
    }


def _extract_track(item: dict) -> dict | None:
    track = item.get("track") or {}
    if not track.get("id"):
        track_id = item.get("track_id")
        if track_id:
            track = {"id": track_id}
        else:
            return None
    return track


def run_sync_beatport(
    dry_run: bool,
    verbose: bool,
    limit: int,
    playlist: str | None = None,
) -> None:
    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] — no changes will be made")

    token = _get_token()
    http_client = bp_api.make_client(token)

    def on_401() -> None:
        nonlocal token
        new_token = _try_refresh()
        if new_token:
            console.print("[dim]Token refreshed.[/dim]")
            token = new_token
            http_client.headers["authorization"] = token
        else:
            raise bp_api.AuthExpiredError(
                "Beatport token expired and refresh failed. Update BEATPORT_ACCESS_TOKEN in .env."
            )

    beatport = bp_api.Beatport(client=http_client, on_401=on_401)

    console.print("Fetching Beatport playlists…")
    try:
        all_playlists = beatport.list_my_playlists()
    except bp_api.AuthExpiredError as e:
        console.print(f"[red]Auth failed:[/red] {e}")
        sys.exit(1)

    if not all_playlists:
        console.print("[dim]No playlists found.[/dim]")
        return

    if playlist:
        playlists = [p for p in all_playlists if p.get("name") == playlist]
        if not playlists:
            names = ", ".join(f'"{p.get("name")}"' for p in all_playlists)
            console.print(f'[red]Playlist "{playlist}" not found.[/red] Available: {names}')
            sys.exit(1)
        console.print(f'Syncing playlist [bold]"{playlist}"[/bold]')
    else:
        playlists = all_playlists
        console.print(f"Found [bold]{len(playlists)}[/bold] playlists")

    total_new = 0
    total_skipped = 0
    total_failed = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        for pl in playlists:
            pl_id = pl.get("id")
            pl_name = pl.get("name", f"Playlist {pl_id}")
            task = progress.add_task(f"[cyan]{pl_name}[/cyan]", total=None)

            try:
                items = beatport.list_playlist_items(pl_id)
            except bp_api.AuthExpiredError as e:
                progress.stop()
                console.print(f"[red]Auth failed:[/red] {e}")
                sys.exit(1)
            except Exception as e:
                progress.log(f"[red]Failed to fetch {pl_name}:[/red] {e}")
                continue

            progress.update(task, total=len(items))
            pl_new = pl_skipped = 0

            local_playlist_id = None if dry_run else detect_db.upsert_beatport_playlist(pl_id, pl_name)

            for item in items:
                progress.update(task, advance=1)
                track = _extract_track(item)
                if not track:
                    total_failed += 1
                    continue

                artists = [a.get("name", "") for a in track.get("artists", [])]
                artist = ", ".join(a for a in artists if a) or "Unknown"
                title = track.get("name") or "Unknown"
                meta = _track_meta(track)

                if not meta["beatport_link"]:
                    total_failed += 1
                    continue

                if limit and total_new >= limit:
                    break

                progress.update(task, description=f"[cyan]{pl_name}[/cyan]  {artist} — {title}")

                if dry_run:
                    total_new += 1
                    pl_new += 1
                    if verbose:
                        progress.log(f"[green]would add:[/green] {artist} — {title}")
                    continue

                acted = detect_db.insert_beatport_track(
                    artist, title, meta["beatport_link"], meta,
                    playlist_id=local_playlist_id,
                )
                if acted:
                    total_new += 1
                    pl_new += 1
                    if verbose:
                        progress.log(f"[green]added:[/green] {artist} — {title}")
                else:
                    total_skipped += 1
                    pl_skipped += 1

            progress.update(task, description=f"[cyan]{pl_name}[/cyan]  +{pl_new} new, {pl_skipped} skipped")

    http_client.close()

    console.print()
    console.print(f"[bold]Sync {'(dry run) ' if dry_run else ''}complete[/bold]")
    console.print(f"  New/linked: {total_new}")
    console.print(f"  Skipped:    {total_skipped}")
    if total_failed:
        console.print(f"  Failed:     {total_failed}")
