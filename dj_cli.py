#!/usr/bin/env python3
"""DJ CLI — unified tool:

  dj export-studio ...   DJ Studio mix → Rekordbox
  dj detect ...          Detect tracks from Instagram/radio/Mixcloud/YouTube/Podbean
  dj sync ...            Sync Apple Music → Beatport playlists

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

from djstudio.display import print_mix_list
from djstudio.extractor import DJStudioMixExtractor
from pipeline import run_full_pipeline
from detect.cli import add_detect_subparser, dispatch as dispatch_detect
from sync.cli import add_sync_subparser, dispatch as dispatch_sync


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dj_cli.py",
        description="DJ Studio → Rekordbox exporter + track detection + Apple Music sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run dj_cli.py export-studio "Ibiza Vibes"
  uv run dj_cli.py export-studio mix.json --pass2-only
  uv run dj_cli.py export-studio --list

  uv run dj_cli.py detect instagram https://www.instagram.com/p/abc123/
  uv run dj_cli.py detect mixcloud https://www.mixcloud.com/djname/mixname/
  uv run dj_cli.py detect history
  uv run dj_cli.py detect enrich --dry-run

  uv run dj_cli.py sync music-beatport sync --library
""",
    )
    sub = parser.add_subparsers(dest="command")

    # ── export-studio ────────────────────────────────────────────────────────
    migrate_p = sub.add_parser(
        "export-studio",
        help="DJ Studio mix → Rekordbox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    migrate_p.add_argument("target", nargs="?", help="Mix name OR path to .json file")
    migrate_p.add_argument("--list", action="store_true", help="List available DJ Studio mixes")
    migrate_p.add_argument("--extract-only", action="store_true",
                           help="Just dump JSON, don't write to rekordbox")
    migrate_p.add_argument("-o", "--output", help="JSON output path (with --extract-only)")
    migrate_p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    migrate_p.add_argument("--no-watch", action="store_true",
                           help="Skip the analysis watch (Pass 1 only)")
    migrate_p.add_argument("--pass1-only", action="store_true",
                           help="Run Pass 1 only (tracks + playlist + effects)")
    migrate_p.add_argument("--pass2-only", action="store_true",
                           help="Skip Pass 1, just watch + Pass 2 (cues)")
    migrate_p.add_argument("--no-snap", action="store_true",
                           help="Pass 2: skip beatgrid snapping (use raw beat positions)")

    # ── detect ────────────────────────────────────────────────────────────────
    detect_p = add_detect_subparser(sub)

    # ── sync ──────────────────────────────────────────────────────────────────
    sync_p = add_sync_subparser(sub)

    # ── login-beatport ────────────────────────────────────────────────────────
    lb_p = sub.add_parser(
        "login-beatport",
        help="Fetch a fresh Beatport token and save it to .env",
    )
    mode = lb_p.add_mutually_exclusive_group()
    mode.add_argument("--ui", action="store_true",
                      help="Browser login with visible window (use if headless is blocked by Cloudflare)")
    mode.add_argument("--cookie", action="store_true",
                      help="Refresh via BEATPORT_SESSION_TOKEN cookie")

    return parser, migrate_p, detect_p, sync_p, lb_p


def _handle_migrate(args, migrate_p: argparse.ArgumentParser) -> int:
    if args.list:
        extractor = DJStudioMixExtractor()
        print_mix_list(extractor.get_all_projects())
        return 0

    if not args.target:
        migrate_p.print_help()
        return 0

    output = Path(args.output) if args.output else None
    return run_full_pipeline(
        args.target,
        dry_run=args.dry_run,
        no_watch=args.no_watch,
        pass1_only=args.pass1_only,
        pass2_only=args.pass2_only,
        no_snap=args.no_snap,
        extract_only=args.extract_only,
        output=output,
    )


def _handle_login_beatport(args) -> None:
    from connections import beatport as bp_api
    from rich.console import Console
    console = Console()

    def _save_and_report(token: str, session: Optional[str] = None) -> None:
        bp_api.save_token_to_env(token, session)
        import os; os.environ["BEATPORT_ACCESS_TOKEN"] = token.removeprefix("Bearer ").strip()
        extra = " + session cookie" if session else ""
        console.print(f"[green]Token{extra} saved to .env[/green] — expires in ~10 min, session cookie will auto-refresh.")

    # If no explicit mode flag, check if current token is still valid — skip refresh if so
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

    # --cookie: session cookie refresh
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

    # --ui or headless: browser login
    if args.ui or not args.cookie:
        import os as _os
        username = _os.environ.get("BEATPORT_USERNAME", "").strip() or None
        password = _os.environ.get("BEATPORT_PASSWORD", "").strip() or None

        # auto-detect: if no --ui and session cookie is available, try cookie first
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
    parser, migrate_p, detect_p, sync_p, lb_p = _build_parser()
    args = parser.parse_args()

    if args.command == "export-studio":
        sys.exit(_handle_migrate(args, migrate_p))
    elif args.command == "detect":
        dispatch_detect(args, detect_p)
    elif args.command == "sync":
        dispatch_sync(args, sync_p)
    elif args.command == "login-beatport":
        _handle_login_beatport(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
