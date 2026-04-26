#!/usr/bin/env python3
"""
Clear Beatport authentication from rekordbox.

Removes the stored Beatport tokens and cached library data so you can
sign in with a different account. Rekordbox must be CLOSED before running.

Usage:
    uv run clear_beatport_auth.py           # dry run — shows what would be deleted
    uv run clear_beatport_auth.py --apply   # actually delete
"""

import argparse
import shutil
import sys
from pathlib import Path

RB6 = Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox6"

# Auth tokens — deleting these forces re-login
AUTH_DIR = RB6 / ".beatport"

# Cached Beatport library data (playlists, tracks, artwork)
CACHE_DIR = RB6 / ".cache" / ".beatport"


def check_rekordbox_running() -> bool:
    import subprocess
    result = subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True)
    return result.returncode == 0


def describe(path: Path) -> str:
    if not path.exists():
        return f"  {path}  (not found, skipping)"
    if path.is_dir():
        files = list(path.rglob("*"))
        size = sum(f.stat().st_size for f in files if f.is_file())
        return f"  {path}  ({len(files)} files, {size // 1024} KB)"
    return f"  {path}  ({path.stat().st_size} bytes)"


def main():
    parser = argparse.ArgumentParser(description="Clear Beatport auth from rekordbox")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry run)")
    args = parser.parse_args()

    dry_run = not args.apply

    if check_rekordbox_running():
        print("ERROR: rekordbox is running. Close it before proceeding.", file=sys.stderr)
        sys.exit(1)

    targets = [AUTH_DIR, CACHE_DIR]

    print("Beatport auth files to remove:\n")
    for t in targets:
        print(describe(t))

    if dry_run:
        print("\nDry run — nothing deleted. Run with --apply to clear.")
        return

    print("\nDeleting...")
    for t in targets:
        if t.exists():
            shutil.rmtree(t)
            print(f"  Deleted: {t}")
        else:
            print(f"  Skipped (not found): {t}")

    print("\nDone. Open rekordbox and sign in to Beatport with your new account.")


if __name__ == "__main__":
    main()
