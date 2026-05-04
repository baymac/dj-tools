"""Argparse CLI for the sync subcommands (music-beatport and detect-beatport)."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from rich.console import Console

from connections import matching

console = Console()

_TOKEN_HINT = (
    "Get fresh tokens:\n"
    "  1. Open beatport.com in a browser (logged in)\n"
    "  2. DevTools → Network → /api/auth/session → copy [bold]token.accessToken[/bold]\n"
    "     → set as BEATPORT_ACCESS_TOKEN in .env\n"
    "  3. DevTools → Application → Cookies → copy [bold]__Secure-next-auth.session-token[/bold]\n"
    "     → set as BEATPORT_SESSION_TOKEN in .env"
)


def add_sync_subparser(parent) -> argparse.ArgumentParser:
    sync_p = parent.add_parser("sync", help="Sync music platforms to Beatport playlists.")
    sync_sub = sync_p.add_subparsers(dest="sync_command")
    sync_sub.required = False

    # ── music-beatport ──────────────────────────────────────────────────────
    mb_p = sync_sub.add_parser(
        "music-beatport",
        help="Apple Music → Beatport playlist sync.",
    )
    mb_sub = mb_p.add_subparsers(dest="sync_music_command")
    mb_sub.required = False

    mb_sub.add_parser("check-connections", help="Verify MusicKit authorization and Beatport credentials.")

    mb_sub.add_parser("list-playlists", help="List Apple Music playlists available for sync.")

    mb_sync_p = mb_sub.add_parser("sync", help="Sync an Apple Music source to Beatport genre playlists.")
    mb_sync_p.add_argument("--playlist", "-p", default=None, metavar="NAME",
                           help="Apple Music playlist name to sync.")
    mb_sync_p.add_argument("--library", dest="use_library", action="store_true",
                           help="Sync songs added to library (Music app 'Songs' tab).")
    mb_sync_p.add_argument("--favorites", dest="use_favorites", action="store_true",
                           help="Sync songs in the 'Favourite Songs' playlist.")
    mb_sync_p.add_argument("--library-and-favorites", dest="use_lib_and_fav", action="store_true",
                           help="Sync library songs plus Favourite Songs (union).")
    mb_sync_p.add_argument("--all", dest="use_all", action="store_true",
                           help="Sync all songs from MusicLibraryRequest<Song> (no filter).")
    mb_sync_p.add_argument("--dry-run", action="store_true",
                           help="Show what would be synced without making changes.")
    mb_sync_p.add_argument("--limit", type=int, default=0, metavar="N",
                           help="Stop after processing N tracks (0 = no limit).")
    mb_sync_p.add_argument("--verbose", "-v", action="store_true",
                           help="Print Beatport search details to stderr.")
    mb_sync_p.add_argument("--threshold", type=float, default=matching.MATCH_THRESHOLD,
                           metavar="F", help=f"Fuzzy match threshold 0-1 (default: {matching.MATCH_THRESHOLD}).")

    return sync_p


def dispatch(args, sync_p: argparse.ArgumentParser) -> None:
    from connections import beatport as api, musickit
    from sync import db as sdb, sync as S

    sdb.init_db()

    if not args.sync_command:
        sync_p.print_help()
        return

    # ── music-beatport ───────────────────────────────────────────────────────
    if args.sync_command == "music-beatport":
        sync_music_command = getattr(args, "sync_music_command", None)
        if not sync_music_command:
            # find the music-beatport subparser and print its help
            for action in sync_p._subparsers._group_actions:
                for choice, sub in action.choices.items():
                    if choice == "music-beatport":
                        sub.print_help()
                        break
            return

        if sync_music_command == "check-connections":
            console.print("Checking MusicKit…", end=" ")
            authorized, msg = musickit.check_musickit()
            if authorized:
                console.print("[green]OK[/green]")
            else:
                console.print(f"[red]FAILED[/red]\n{msg}")

            console.print("Checking Beatport…", end=" ")
            try:
                beatport, client = S.make_bp_client()
                playlists = beatport.list_my_playlists()
                console.print(f"[green]OK[/green] ({len(playlists)} playlists found)")
                client.close()
            except SystemExit:
                raise
            except Exception as e:
                console.print(f"[red]FAILED[/red]\n{e}")
                if "401" in str(e):
                    console.print(f"\n[yellow]{_TOKEN_HINT}[/yellow]")
            return

        if sync_music_command == "list-playlists":
            console.print("Fetching playlists from Apple Music…")
            try:
                names = musickit.list_playlists()
            except RuntimeError as e:
                console.print(f"[red]Error:[/red] {e}")
                sys.exit(1)
            for name in sorted(names):
                console.print(f"  {name}")
            console.print(f"\n[dim]{len(names)} playlists[/dim]")
            return

        if sync_music_command == "sync":
            mode_flags = sum([
                bool(args.playlist),
                args.use_library,
                args.use_favorites,
                args.use_lib_and_fav,
                args.use_all,
            ])
            if mode_flags > 1:
                console.print("[red]Error:[/red] --playlist, --library, --favorites, "
                              "--library-and-favorites, and --all are mutually exclusive.")
                sys.exit(1)
            S.run_sync(
                playlist=args.playlist,
                use_library=args.use_library,
                use_favorites=args.use_favorites,
                use_lib_and_fav=args.use_lib_and_fav,
                use_all=args.use_all,
                dry_run=args.dry_run,
                limit=args.limit,
                verbose=args.verbose,
                threshold=args.threshold,
            )
            return

