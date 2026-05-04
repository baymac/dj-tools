"""
Clear Apple Music library — removes all tracks from library and all user playlists.

DESTRUCTIVE — this cannot be undone without a backup + restore.
Always run backup_apple_music.py first.

Requires typing "DELETE MY LIBRARY" at the prompt to proceed.

Usage:
    uv run python helpers/clear_apple_music.py
    uv run python helpers/clear_apple_music.py --favourites-only   # only remove loved tracks
    uv run python helpers/clear_apple_music.py --dry-run            # show what would happen
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def run_applescript(script: str, timeout: int = 600) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        sys.exit(f"AppleScript error: {r.stderr.strip()}")
    return r.stdout.strip()


def count_library() -> int:
    result = run_applescript(
        'tell application "Music" to return count of tracks in library playlist 1'
    )
    try:
        return int(result.strip())
    except ValueError:
        return 0


def get_track_count_in_playlist(name: str) -> int:
    script = f"""
tell application "Music"
    try
        return count of tracks in playlist "{name}"
    on error
        return 0
    end try
end tell
"""
    try:
        return int(run_applescript(script).strip())
    except ValueError:
        return 0


def clear_library_in_batches(batch_size: int = 50, dry_run: bool = False) -> None:
    """Delete all library tracks in batches to avoid AppleScript timeout."""
    total = count_library()
    print(f"[clear] {total} tracks in library", flush=True)

    if dry_run:
        print(f"[clear] DRY RUN — would delete {total} tracks", flush=True)
        return

    deleted = 0
    while True:
        remaining = count_library()
        if remaining == 0:
            break
        take = min(batch_size, remaining)
        script = f"""
tell application "Music"
    set trackList to (tracks 1 through {take} of library playlist 1)
    repeat with t in trackList
        delete t
    end repeat
end tell
"""
        run_applescript(script, timeout=120)
        deleted += take
        print(f"[clear] deleted {deleted}/{total}…", flush=True)
        time.sleep(0.5)

    print(f"[clear] library cleared", flush=True)


def clear_favourite_songs(dry_run: bool = False) -> None:
    count = get_track_count_in_playlist("Favourite Songs")
    print(f"[clear] {count} tracks in Favourite Songs", flush=True)

    if dry_run:
        print(f"[clear] DRY RUN — would remove {count} tracks from Favourite Songs", flush=True)
        return

    if count == 0:
        return

    script = """
tell application "Music"
    try
        delete playlist "Favourite Songs"
    on error e
        return "error: " & e
    end try
    return "done"
end tell
"""
    run_applescript(script)
    print("[clear] Favourite Songs playlist deleted (tracks still in library)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--favourites-only", action="store_true",
                    help="only clear Favourite Songs, leave rest of library intact")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be deleted without actually deleting")
    args = ap.parse_args()

    print("=" * 60)
    print("  APPLE MUSIC LIBRARY CLEAR")
    print("=" * 60)

    if args.dry_run:
        print("\n  DRY RUN MODE — nothing will be deleted\n")
    else:
        print("\n  WARNING: This will permanently remove tracks from")
        print("  your Apple Music library. Make sure you have run")
        print("  backup_apple_music.py first.\n")
        print('  Type  DELETE MY LIBRARY  to confirm, or Ctrl+C to abort:')
        try:
            answer = input("  > ").strip()
        except KeyboardInterrupt:
            print("\n  Aborted.")
            sys.exit(0)
        if answer != "DELETE MY LIBRARY":
            print("  Confirmation not matched. Aborted.")
            sys.exit(0)
        print()

    if args.favourites_only:
        clear_favourite_songs(dry_run=args.dry_run)
    else:
        clear_favourite_songs(dry_run=args.dry_run)
        clear_library_in_batches(dry_run=args.dry_run)

    print("\n[clear] done.")


if __name__ == "__main__":
    main()
