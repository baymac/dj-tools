"""Idempotent push: Stage 6a of the enrichment pipeline.

Reads tracks from enriched_tracks_full where rekordbox_export_at IS NULL and
pushes them to a rekordbox playlist via `playlist.to_rekordbox`. Marks each
track's `rekordbox_export_at` on success so re-runs only pick up new rows.

For ad-hoc curated playlist pushes, use `dj playlist rekordbox` instead — it
takes a SQL query and doesn't touch the pipeline timestamps.
"""
from __future__ import annotations

from rich.console import Console

from detect import db as detect_db
from playlist.to_rekordbox import push_to_rekordbox

console = Console()


def export_to_rekordbox(
    *,
    playlist_name: str = "DJ Tools - Enrich",
    limit: int = 0,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    from paths import command_logger
    with command_logger("export-to-rekordbox", console) as log_path:
        console.print(f"[dim]Log: {log_path}[/dim]")
        _export_to_rekordbox_impl(
            playlist_name=playlist_name, limit=limit, dry_run=dry_run, force=force,
        )


def _export_to_rekordbox_impl(
    *, playlist_name: str, limit: int, dry_run: bool, force: bool,
) -> None:
    detect_db.migrate()
    pending = detect_db.get_export_to_rekordbox_pending(force=force)
    rows = [dict(r) for r in pending]
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print(
            "Nothing to export — every track in enriched_tracks_full already has rekordbox_export_at set.\n"
            "[dim]Use --force to re-push all tracks.[/dim]"
        )
        return

    console.print(
        f"[bold]export-to-rekordbox[/bold] ← {len(rows)} pending"
        f"{' [yellow](forced)[/yellow]' if force else ''}"
    )

    push_to_rekordbox(
        rows, playlist_name,
        dry_run=dry_run,
        on_added=lambda bid: detect_db.mark_pipeline_done(bid, "rekordbox_export_at"),
        console=console,
    )

    if not dry_run:
        console.print(
            f"\n[dim]Next:[/dim] open rekordbox → find the [yellow]{playlist_name}[/yellow] "
            "playlist → right-click → [cyan]Analyze Tracks[/cyan]. When done, "
            "run [cyan]dj detect import-rekordbox-analysis[/cyan] to ingest the phrase + cue data."
        )
