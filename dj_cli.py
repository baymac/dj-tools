#!/usr/bin/env python3
"""DJ CLI — unified tool:

  dj detect ...          Detect tracks from Instagram/radio/Mixcloud/YouTube/Podbean
  dj sync ...            Sync Apple Music → Beatport playlists
  dj playlist ...        Push a SQL query of enriched tracks to Beatport / rekordbox / DJ Studio
  dj login-beatport ...  Refresh Beatport tokens

Run `uv run dj_cli.py <command> --help` for details.
"""

import argparse
import sys
import warnings
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", message=".*audioop.*", category=DeprecationWarning)

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from detect.cli import add_detect_subparser, dispatch as dispatch_detect
from sync.cli import add_sync_subparser, dispatch as dispatch_sync
from playlist.cli import add_playlist_subparser, dispatch as dispatch_playlist


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="dj_cli.py",
        description="Track detection + Apple Music sync + curated playlist pushes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run dj_cli.py detect instagram https://www.instagram.com/p/abc123/
  uv run dj_cli.py detect mixcloud https://www.mixcloud.com/djname/mixname/
  uv run dj_cli.py detect history
  uv run dj_cli.py detect enrich --dry-run

  uv run dj_cli.py sync music-beatport sync --library

  uv run dj_cli.py playlist beatport --query "SELECT beatport_id FROM enriched_tracks_full WHERE genre='Tech House' AND mik_nrg>=7" --name "Peak Tech House"
  uv run dj_cli.py playlist rekordbox --query "..." --name "..."
""",
    )
    sub = parser.add_subparsers(dest="command")

    detect_p = add_detect_subparser(sub)
    sync_p = add_sync_subparser(sub)
    playlist_p = add_playlist_subparser(sub)

    lb_p = sub.add_parser(
        "login-beatport",
        help="Fetch a fresh Beatport token and save it to .env",
    )
    mode = lb_p.add_mutually_exclusive_group()
    mode.add_argument("--ui", action="store_true",
                      help="Browser login with visible window (use if headless is blocked by Cloudflare)")
    mode.add_argument("--cookie", action="store_true",
                      help="Refresh via BEATPORT_SESSION_TOKEN cookie")
    mode.add_argument("--cdp", action="store_true",
                      help="Attach to running Brave via CDP (port 9222) and run the auth call from "
                           "inside Brave itself — bypasses every Cloudflare fingerprint check. "
                           "Requires Brave launched with --remote-debugging-port=9222.")

    return parser, detect_p, sync_p, playlist_p, lb_p


def _handle_login_beatport(args) -> None:
    from connections import beatport as bp_api
    from rich.console import Console
    console = Console()

    def _save_and_report(token: str, session: Optional[str] = None) -> None:
        bp_api.save_token_to_env(token, session)
        import os; os.environ["BEATPORT_ACCESS_TOKEN"] = token.removeprefix("Bearer ").strip()
        extra = " + session cookie" if session else ""
        console.print(f"[green]Token{extra} saved to .env[/green] — expires in ~10 min, session cookie will auto-refresh.")

    if not args.ui and not args.cookie:
        import os, time as _t
        current = os.environ.get("BEATPORT_ACCESS_TOKEN", "").strip()
        if current:
            if not current.startswith("Bearer "):
                current = f"Bearer {current}"
            remaining = int(bp_api._jwt_payload(current).get("exp", 0) - _t.time())
            if remaining > 0:
                console.print(f"[green]Token still valid[/green] — expires in {remaining}s. Use --cookie or --ui to force refresh.")
                return

    if args.cookie:
        session_cookie = __import__("os").environ.get("BEATPORT_SESSION_TOKEN", "").strip()
        if not session_cookie:
            console.print("[red]BEATPORT_SESSION_TOKEN not set in .env[/red]")
            sys.exit(1)
        token = bp_api.refresh_via_session(session_cookie)
        if not token:
            console.print("[red]Session cookie refresh failed — cookie may be expired.[/red]")
            sys.exit(1)
        _save_and_report(token)
        return

    if args.cdp:
        console.print("[dim]Connecting to running Brave via CDP (localhost:9222) and fetching access token from inside Brave…[/dim]")
        try:
            token = bp_api.capture_session_via_cdp()
        except RuntimeError as e:
            console.print(f"[red]CDP login failed:[/red] {e}")
            sys.exit(1)
        _save_and_report(token)
        return

    if args.ui or not args.cookie:
        import os as _os
        username = _os.environ.get("BEATPORT_USERNAME", "").strip() or None
        password = _os.environ.get("BEATPORT_PASSWORD", "").strip() or None

        if not args.ui:
            session_cookie = _os.environ.get("BEATPORT_SESSION_TOKEN", "").strip()
            if session_cookie:
                token = bp_api.refresh_via_session(session_cookie)
                if token:
                    _save_and_report(token)
                    return
                console.print("[yellow]Session cookie refresh failed — falling back to browser login…[/yellow]")

        headless = not args.ui
        console.print(f"[dim]Opening {'visible' if not headless else 'headless'} browser to grab Beatport session…[/dim]")
        try:
            token, session = bp_api.capture_token(username, password, headless=headless)
        except RuntimeError as e:
            console.print(f"[red]Login failed:[/red] {e}")
            sys.exit(1)
        _save_and_report(token, session)


def main() -> None:
    parser, detect_p, sync_p, playlist_p, lb_p = _build_parser()
    args = parser.parse_args()

    if args.command == "detect":
        dispatch_detect(args, detect_p)
    elif args.command == "sync":
        dispatch_sync(args, sync_p)
    elif args.command == "playlist":
        dispatch_playlist(args, playlist_p)
    elif args.command == "login-beatport":
        _handle_login_beatport(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
