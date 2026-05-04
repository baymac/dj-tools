"""Argparse CLI for `dj playlist <destination> --query --name`."""
from __future__ import annotations

import argparse
import sys

from rich.console import Console

console = Console()


def add_playlist_subparser(parent) -> argparse.ArgumentParser:
    p = parent.add_parser(
        "playlist",
        help="Push a SQL query of enriched tracks to Beatport / rekordbox / DJ Studio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example queries:\n"
            "  --query \"SELECT beatport_id FROM enriched_tracks_full "
            "WHERE genre='Tech House' AND mik_nrg>=7 ORDER BY tempo_precise\"\n"
            "  --query \"SELECT beatport_id FROM enriched_tracks "
            "WHERE bpm BETWEEN 124 AND 128\"\n"
            "  --query \"SELECT beatport_id FROM enriched_tracks_full "
            "WHERE rk_analysis_json LIKE '%\\\"mood_name\\\":\\\"High%' LIMIT 30\""
        ),
    )
    sub = p.add_subparsers(dest="playlist_command")

    for dest_name, dest_help in [
        ("beatport", "Create or append to a Beatport playlist."),
        ("rekordbox", "Create or append to a rekordbox playlist."),
        ("dj-studio", "Write a new DJ Studio mix project file."),
    ]:
        d = sub.add_parser(dest_name, help=dest_help)
        d.add_argument(
            "--query", "-q", required=True,
            help="SQL query against enriched_tracks_full or enriched_tracks. Must SELECT beatport_id.",
        )
        d.add_argument(
            "--name", "-n", required=True,
            help="Destination playlist or mix name.",
        )
        d.add_argument("--dry-run", action="store_true",
                       help="Show what would happen without writing.")

    return p


def dispatch(args, p: argparse.ArgumentParser) -> None:
    cmd = getattr(args, "playlist_command", None)
    if not cmd:
        p.print_help()
        return

    from paths import command_logger

    with command_logger(f"playlist-{cmd}", console) as log_path:
        console.print(f"[dim]Log: {log_path}[/dim]")
        _dispatch_impl(args, p, cmd)


def _dispatch_impl(args, p: argparse.ArgumentParser, cmd: str) -> None:
    from playlist.query import run_user_query, fetch_full_rows

    try:
        beatport_ids = run_user_query(args.query)
    except ValueError as e:
        console.print(f"[red]Query error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]SQL error:[/red] {e}")
        sys.exit(1)

    if not beatport_ids:
        console.print("[yellow]Query returned no rows.[/yellow]")
        return

    console.print(f"[dim]Query → {len(beatport_ids)} unique beatport_ids[/dim]")

    rows = fetch_full_rows(beatport_ids)
    if len(rows) < len(beatport_ids):
        console.print(
            f"[yellow]{len(beatport_ids) - len(rows)} of {len(beatport_ids)} beatport_ids "
            f"have no row in enriched_tracks_full[/yellow]"
        )

    if cmd == "beatport":
        from playlist.to_beatport import push_to_beatport
        push_to_beatport(beatport_ids, args.name, dry_run=args.dry_run, console=console)
    elif cmd == "rekordbox":
        from playlist.to_rekordbox import push_to_rekordbox
        push_to_rekordbox(rows, args.name, dry_run=args.dry_run, console=console)
    elif cmd == "dj-studio":
        from playlist.to_djstudio import push_to_djstudio
        push_to_djstudio(rows, args.name, dry_run=args.dry_run, console=console)
    else:
        p.print_help()
