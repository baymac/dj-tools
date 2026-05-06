"""Update KeyID on Beatport-streaming tracks in a rekordbox playlist using the
`mik_key` we computed in `enriched_tracks_analysis`.

Subset of the studio-analyse pipeline: studio-analyse populates our DB, then
this helper takes any rekordbox playlist of Beatport tracks and rewrites the
KeyID column to point at the rekordbox DjmdKey row whose ScaleName matches our
analyzed Camelot key. Beatport's metadata key is overwritten in place — that's
what the user-facing rekordbox key column will display going forward.

Usage:
    uv run helpers/update_rekordbox_keys.py --playlist "GF Birthday Set 4"
    uv run helpers/update_rekordbox_keys.py --playlist "Foo" --dry-run

Requires:
    - rekordbox quit (master.db is locked while it's open)
    - tracks already analyzed (have a row in enriched_tracks_analysis with
      mik_key set)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from rich.console import Console

from rekordbox.backup import backup_db
from rekordbox.utils import beatport_id_from_folder_path, is_rekordbox_running

console = Console()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--playlist", required=True,
                    help="Rekordbox playlist name (exact match)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if is_rekordbox_running():
        console.print(
            "[red]rekordbox is running.[/red] Quit it first — it locks master.db."
        )
        return 1

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    # Build mik_key lookup from our DB
    con = sqlite3.connect(Path.home() / "Music" / "dj-tools" / "dj.db")
    con.row_factory = sqlite3.Row
    bp_to_mikkey = {
        r["beatport_id"]: r["mik_key"]
        for r in con.execute(
            "SELECT beatport_id, mik_key FROM enriched_tracks_analysis WHERE mik_key IS NOT NULL"
        )
    }
    con.close()
    console.print(f"[dim]Have mik_key for {len(bp_to_mikkey)} tracks in our DB[/dim]")

    db = Rekordbox6Database()
    try:
        pl = db.get_playlist(Name=args.playlist).first()
        if not pl:
            console.print(f"[red]Playlist not found:[/red] {args.playlist}")
            return 1

        # Build ScaleName → KeyID map for rekordbox's DjmdKey table
        key_lookup = {k.ScaleName: k.ID for k in db.session.query(tables.DjmdKey).all()}
        console.print(f"[dim]rekordbox DjmdKey: {len(key_lookup)} entries[/dim]")

        songs = db.get_playlist_songs(PlaylistID=pl.ID).all()
        console.print(
            f"[bold]Updating keys[/bold] in playlist '{args.playlist}' "
            f"({len(songs)} tracks)"
        )
        if args.dry_run:
            console.print("[dim]DRY RUN — no writes[/dim]")

        if not args.dry_run:
            backup = backup_db(f"keyupdate_{args.playlist.replace(' ', '_')}")
            if backup is None:
                console.print("[red]Backup failed — aborting.[/red]")
                return 1
            console.print(f"[dim]Backed up master.db → {backup}[/dim]")

        counts = {"updated": 0, "unchanged": 0, "no_mik_key": 0,
                  "not_beatport": 0, "no_key_row": 0}

        for s in songs:
            c = db.session.query(tables.DjmdContent).filter(
                tables.DjmdContent.ID == s.ContentID
            ).first()
            bid = beatport_id_from_folder_path(c.FolderPath if c else None)
            if bid is None:
                counts["not_beatport"] += 1
                continue

            mik_key = bp_to_mikkey.get(bid)
            if not mik_key:
                counts["no_mik_key"] += 1
                if args.verbose:
                    console.print(f"  [dim]bp:{bid} skip — no mik_key in our DB ({c.Title})[/dim]")
                continue

            target_key_id = key_lookup.get(mik_key)
            if not target_key_id:
                counts["no_key_row"] += 1
                console.print(f"  [yellow]bp:{bid} no DjmdKey row for ScaleName='{mik_key}' (skipped)[/yellow]")
                continue

            if str(c.KeyID) == str(target_key_id):
                counts["unchanged"] += 1
                continue

            old_key = next((sn for sn, kid in key_lookup.items() if str(kid) == str(c.KeyID)), "?")
            counts["updated"] += 1
            if args.verbose or args.dry_run:
                console.print(
                    f"  bp:{bid:<10}  {old_key:>4} → {mik_key:<4}  {(c.Title or '?')[:60]}"
                )
            if not args.dry_run:
                c.KeyID = target_key_id

        if not args.dry_run:
            db.commit()

        console.print()
        console.print(
            f"[bold]{'Dry run' if args.dry_run else 'Done'}.[/bold]  "
            f"updated: [green]{counts['updated']}[/green]  "
            f"unchanged: {counts['unchanged']}  "
            f"no mik_key in DB: {counts['no_mik_key']}  "
            f"missing DjmdKey row: {counts['no_key_row']}  "
            f"not Beatport: {counts['not_beatport']}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
