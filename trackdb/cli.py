"""Wire `dj db ...` argparse subparsers into top-level CLI."""

import argparse

from . import commands as C
from .schema import INTENSITY_LEVELS, SECTION_TYPES, get_db, init_db


def add_db_subparser(parent: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach `db` and its subcommands to the parent subparsers."""
    db_p = parent.add_parser(
        "db",
        help="Track metadata database (energy, vocals/drums/melody, sections)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Section types : intro  buildup  drop  breakdown  outro  bridge  verse  chorus
Intensity     : none   low      medium   high

Examples:
  uv run dj_cli.py db populate
  uv run dj_cli.py db show beatport-sdk_12345678
  uv run dj_cli.py db update beatport-sdk_12345678 --energy 8 --vocals high
  uv run dj_cli.py db section add beatport-sdk_12345678 drop 128 256
  uv run dj_cli.py db section list beatport-sdk_12345678
""",
    )
    sub = db_p.add_subparsers(dest="db_command")

    populate_p = sub.add_parser(
        "populate",
        help="Import tracks from a DJ Studio mix (uses latest revision if duplicates)",
    )
    populate_p.add_argument(
        "mix_name",
        help="DJ Studio mix name (latest revision picked when duplicates exist)",
    )
    populate_p.add_argument(
        "--fetch-release-dates",
        dest="fetch_release_dates",
        action="store_true",
        help="After populating, fetch release dates from Beatport for tracks that don't have one",
    )

    sub.add_parser("list", help="List all tracks")

    show_p = sub.add_parser("show", help="Show track details")
    show_p.add_argument("library_key")

    update_p = sub.add_parser("update", help="Update track metadata")
    update_p.add_argument("library_key")
    update_p.add_argument("--energy", type=int, metavar="1-10")
    update_p.add_argument("--vocals", choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--drums",  choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--melody", choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--key",          help="Camelot key override (e.g. 11A)")
    update_p.add_argument("--bpm",          type=float)
    update_p.add_argument("--notes",        help="Free-form notes")
    update_p.add_argument("--beatport-url", dest="beatport_url", help="Full Beatport track URL")
    update_p.add_argument(
        "--release-date",
        dest="release_date",
        metavar="YYYY-MM-DD",
        help="Release date (skipped if track already has one)",
    )

    section_p = sub.add_parser("section", help="Manage section markers")
    sec_sub = section_p.add_subparsers(dest="section_command")

    sec_add = sec_sub.add_parser("add", help="Add a section marker")
    sec_add.add_argument("library_key")
    sec_add.add_argument("type", choices=sorted(SECTION_TYPES), metavar="TYPE")
    sec_add.add_argument("start_beat", type=float, metavar="START_BEAT")
    sec_add.add_argument("end_beat",   type=float, nargs="?", metavar="END_BEAT")
    sec_add.add_argument("--notes")

    sec_list = sec_sub.add_parser("list", help="List sections for a track")
    sec_list.add_argument("library_key")

    sec_rm = sec_sub.add_parser("remove", help="Remove a section")
    sec_rm.add_argument("section_id", type=int)

    return db_p


def dispatch(args, db_parser: argparse.ArgumentParser) -> None:
    """Dispatch a parsed `dj db ...` invocation."""
    if not args.db_command:
        db_parser.print_help()
        return

    conn = get_db()
    init_db(conn)
    try:
        if args.db_command == "populate":
            C.cmd_populate(conn, args)
        elif args.db_command == "list":
            C.cmd_list(conn, args)
        elif args.db_command == "show":
            C.cmd_show(conn, args)
        elif args.db_command == "update":
            C.cmd_update(conn, args)
        elif args.db_command == "section":
            section_action = getattr(args, "section_command", None)
            if not section_action:
                print("section requires a subcommand: add | list | remove")
                return
            if section_action == "add":
                C.cmd_section_add(conn, args)
            elif section_action == "list":
                C.cmd_section_list(conn, args)
            elif section_action == "remove":
                C.cmd_section_remove(conn, args)
    finally:
        conn.close()
