"""
Delete a track from a Beatport playlist.

Usage:
    uv run python helpers/delete_beatport_track.py \\
        --track https://www.beatport.com/track/how-does-it-feel/26895695 \\
        --playlist "Tech House"

    # or pass the numeric track ID directly
    uv run python helpers/delete_beatport_track.py --track 26895695 --playlist "Tech House"

Requires BEATPORT_USERNAME and BEATPORT_PASSWORD environment variables.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connections import beatport as api
from sync import db, sync


def parse_track_id(value: str) -> int:
    """Accept a full Beatport track URL or a bare numeric ID."""
    m = re.search(r"/(\d+)(?:/|$)", value)
    if m:
        return int(m.group(1))
    if value.isdigit():
        return int(value)
    sys.exit(f"Error: cannot extract a track ID from {value!r}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delete a track from a Beatport playlist."
    )
    ap.add_argument(
        "--track", "-t", required=True,
        help="Beatport track URL or numeric track ID.",
    )
    ap.add_argument(
        "--playlist", "-p", required=True,
        help="Exact name of the Beatport playlist to remove the track from.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be deleted without making changes.",
    )
    args = ap.parse_args()

    track_id = parse_track_id(args.track)
    playlist_name = args.playlist

    print(f"Track ID : {track_id}")
    print(f"Playlist : {playlist_name!r}")

    db.init_db()
    username, password = sync.require_env()
    beatport, http_client = sync.make_bp_client(username, password)

    try:
        playlists = beatport.list_my_playlists()
        name_to_id = {pl["name"]: pl["id"] for pl in playlists}

        if playlist_name not in name_to_id:
            available = ", ".join(sorted(name_to_id))
            sys.exit(
                f"Error: playlist {playlist_name!r} not found.\n"
                f"Available playlists: {available}"
            )

        playlist_id = name_to_id[playlist_name]

        if args.dry_run:
            existing_ids = beatport.list_track_ids(playlist_id)
            if track_id not in existing_ids:
                sys.exit(f"Error: track {track_id} is not in playlist {playlist_name!r}.")
            print(f"DRY RUN — would delete track {track_id} from {playlist_name!r}.")
            return

        try:
            beatport.delete_track(playlist_id, track_id)
        except ValueError as exc:
            sys.exit(f"Error: {exc}")
        print(f"Deleted track {track_id} from {playlist_name!r}.")
    finally:
        http_client.close()


if __name__ == "__main__":
    main()
