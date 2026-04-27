#!/usr/bin/env python3
"""Wipe a rekordbox playlist created by `dj_cli.py migrate`.

Removes the playlist, clears every hot cue and the Commnt field on each track in
it, and (optionally) deletes Beatport streaming entries that are not in any other
playlist. Useful for re-running a migration cleanly.

Usage:
    uv run helpers/cleanup_playlist.py "Ibiza Vibes"                 # default: keep tracks
    uv run helpers/cleanup_playlist.py "Ibiza Vibes" --delete-tracks # also delete created tracks
    uv run helpers/cleanup_playlist.py "Ibiza Vibes" --dry-run       # preview only
    uv run helpers/cleanup_playlist.py --list                        # list playlists

Rekordbox MUST be closed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root on sys.path so we can import from the rekordbox package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables

from rekordbox.backup import backup_db, rekordbox_running


def _ensure_rekordbox_closed() -> None:
    if rekordbox_running():
        print("ERROR: rekordbox is running. Close it before running this script.", file=sys.stderr)
        sys.exit(1)


def _list_playlists(db: Rekordbox6Database) -> None:
    rows = db.get_playlist().all()
    print(f"\n{len(rows)} playlists:\n")
    for p in sorted(rows, key=lambda x: getattr(x, "Name", "") or ""):
        name = getattr(p, "Name", "?") or "?"
        try:
            count = len(db.get_playlist_contents(p).all())
        except Exception:
            count = "?"
        print(f"  [{p.ID}]  {name}  ({count} tracks)")


def _other_playlists_for_content(db: Rekordbox6Database, content_id: str, current_playlist_id: str) -> list:
    """Return playlist IDs (other than current) that contain this content."""
    rows = db.get_playlist_songs(ContentID=content_id).all()
    return [r.PlaylistID for r in rows if r.PlaylistID != current_playlist_id]


def cleanup(name: str, delete_tracks: bool, dry_run: bool) -> int:
    db = Rekordbox6Database()
    try:
        playlist = db.get_playlist(Name=name).first()
        if playlist is None:
            print(f"Playlist '{name}' not found.")
            print("Run with --list to see available playlists.")
            return 1

        contents = db.get_playlist_contents(playlist).all()
        print(f"\nPlaylist: '{name}' (ID={playlist.ID})")
        print(f"  {len(contents)} tracks")

        # Walk tracks, collect what we'd do
        actions = []
        for c in contents:
            cues = db.get_cue(ContentID=c.ID).all()
            hot_cues = [q for q in cues if q.is_hot_cue]
            file_type = getattr(c, "FileType", None)
            commnt = getattr(c, "Commnt", "") or ""
            others = _other_playlists_for_content(db, c.ID, playlist.ID)
            will_delete = (
                delete_tracks and file_type == 20 and not others
            )
            actions.append({
                "content": c,
                "title": f"{getattr(c, 'Title', '?') or '?'}",
                "file_type": file_type,
                "hot_cues": hot_cues,
                "had_comment": bool(commnt),
                "other_playlists": others,
                "will_delete": will_delete,
            })

        # Report
        n_streaming = sum(1 for a in actions if a["file_type"] == 20)
        n_cues = sum(len(a["hot_cues"]) for a in actions)
        n_comments = sum(1 for a in actions if a["had_comment"])
        n_to_delete = sum(1 for a in actions if a["will_delete"])
        n_kept_other_playlists = sum(
            1 for a in actions
            if delete_tracks and a["file_type"] == 20 and a["other_playlists"]
        )

        print(f"  {n_streaming} Beatport streaming entries (FileType=20)")
        print(f"  {n_cues} hot cues will be cleared")
        print(f"  {n_comments} Commnt fields will be cleared")
        if delete_tracks:
            print(f"  {n_to_delete} tracks will be deleted")
            if n_kept_other_playlists:
                print(f"  {n_kept_other_playlists} streaming tracks kept (also in other playlists)")
        else:
            print(f"  Tracks will be kept (use --delete-tracks to also delete streaming entries)")

        if dry_run:
            print("\n--- DRY RUN — no changes made ---\n")
            for a in actions:
                tag = "DELETE" if a["will_delete"] else "KEEP  "
                print(f"  [{tag}] {a['content'].ID}  {a['title']}  (FT={a['file_type']}, cues={len(a['hot_cues'])})")
            return 0

        # Apply — refuse to proceed without a verified backup
        bp = backup_db(f"cleanup_{name}")
        if not bp:
            print("\nERROR: backup_db() returned None — refusing to delete without a backup.", file=sys.stderr)
            print("Check that pyrekordbox can locate master.db (rekordbox/constants.py).", file=sys.stderr)
            return 1
        print(f"\nBackup: {bp.name}")

        for a in actions:
            for cue in a["hot_cues"]:
                db.delete(cue)
            if a["had_comment"]:
                a["content"].Commnt = ""

        db.delete_playlist(playlist)

        if delete_tracks:
            for a in actions:
                if a["will_delete"]:
                    db.delete(a["content"])

        db.commit()
        print(f"\nDone. Removed playlist '{name}', cleared {n_cues} cues and {n_comments} comments.")
        if delete_tracks:
            print(f"Deleted {n_to_delete} tracks.")
        return 0
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wipe a rekordbox playlist created by `dj_cli.py migrate`",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run helpers/cleanup_playlist.py "Ibiza Vibes"
  uv run helpers/cleanup_playlist.py "Ibiza Vibes" --delete-tracks
  uv run helpers/cleanup_playlist.py "Ibiza Vibes" --dry-run
  uv run helpers/cleanup_playlist.py --list
""",
    )
    parser.add_argument("name", nargs="?", help="Playlist name to wipe")
    parser.add_argument("--list", action="store_true", help="List existing playlists")
    parser.add_argument("--delete-tracks", action="store_true",
                        help="Also delete Beatport streaming tracks (FileType=20) "
                             "if not in any other playlist")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    _ensure_rekordbox_closed()

    if args.list:
        db = Rekordbox6Database()
        try:
            _list_playlists(db)
        finally:
            db.close()
        return

    if not args.name:
        parser.print_help()
        sys.exit(1)

    sys.exit(cleanup(args.name, args.delete_tracks, args.dry_run))


if __name__ == "__main__":
    main()
