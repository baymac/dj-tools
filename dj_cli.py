#!/usr/bin/env python3
"""DJ CLI — two functions:

  dj migrate ...   DJ Studio mix → Rekordbox (extract + Pass 1 + watch + Pass 2)
  dj db ...        DJ Studio library → SQLite metadata DB (energy/intensity/sections)

Run `uv run dj_cli.py migrate --help` or `uv run dj_cli.py db --help` for details.
"""

import argparse
import sys
from pathlib import Path

from djstudio.display import print_mix_list
from djstudio.extractor import DJStudioMixExtractor
from pipeline import run_full_pipeline
from rekordbox.backup import undo_list, undo_restore
from trackdb.cli import add_db_subparser, dispatch as dispatch_db


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dj_cli.py",
        description="DJ Studio → Rekordbox migrator + track metadata DB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run dj_cli.py migrate "Ibiza Vibes"            Full pipeline (extract + Pass 1 + watch + Pass 2)
  uv run dj_cli.py migrate "Ibiza Vibes" --extract-only -o mix.json
  uv run dj_cli.py migrate mix.json                 Use existing JSON (Pass 1 + watch + Pass 2)
  uv run dj_cli.py migrate mix.json --pass1-only --dry-run
  uv run dj_cli.py migrate mix.json --pass2-only
  uv run dj_cli.py migrate --list                   List available DJ Studio mixes
  uv run dj_cli.py undo list
  uv run dj_cli.py undo restore 20260427_143200_My_Mix.db

  uv run dj_cli.py db populate
  uv run dj_cli.py db list
  uv run dj_cli.py db show beatport-sdk_12345678
""",
    )
    sub = parser.add_subparsers(dest="command")

    # ── migrate ──────────────────────────────────────────────────────────────
    migrate_p = sub.add_parser(
        "migrate",
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

    # ── db ───────────────────────────────────────────────────────────────────
    db_p = add_db_subparser(sub)

    # ── undo (top-level) ─────────────────────────────────────────────────────
    undo_p = sub.add_parser("undo", help="List or restore from rekordbox DB backups")
    undo_sub = undo_p.add_subparsers(dest="undo_command")
    undo_sub.add_parser("list", help="List available backups")
    undo_restore_p = undo_sub.add_parser("restore", help="Restore from a backup")
    undo_restore_p.add_argument("backup", help="Backup filename (from 'undo list')")

    return parser, migrate_p, db_p, undo_p


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


def main() -> None:
    parser, migrate_p, db_p, undo_p = _build_parser()
    args = parser.parse_args()

    if args.command == "migrate":
        sys.exit(_handle_migrate(args, migrate_p))
    elif args.command == "db":
        dispatch_db(args, db_p)
    elif args.command == "undo":
        cmd = getattr(args, "undo_command", None)
        if cmd == "list":
            undo_list()
        elif cmd == "restore":
            undo_restore(args.backup)
        else:
            undo_p.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
