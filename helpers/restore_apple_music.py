"""
Restore Apple Music library from a backup JSON created by backup_apple_music.py.

What it restores:
  - All library tracks (searches Apple Music catalog by artist + title, adds to library)
  - Loved/favourite status
  - Playlist memberships (creates missing playlists, adds tracks)

Note: Tracks are found by searching the local Music catalog. Tracks not available
on Apple Music (region-locked, removed) will be skipped and logged.

Usage:
    uv run python helpers/restore_apple_music.py --backup ~/Documents/apple_music_backups/backup_2026-04-26_120000.json
    uv run python helpers/restore_apple_music.py --backup <path> --dry-run
    uv run python helpers/restore_apple_music.py --backup <path> --skip-playlists
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run_applescript(script: str, timeout: int = 30) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def escape(s: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def add_track_by_catalog_id(apple_id: str) -> str | None:
    script = f"""
tell application "Music"
    open location "itmss://itunes.apple.com/song?id={apple_id}"
end tell
"""
    run_applescript(script)
    time.sleep(2)
    find_script = f"""
tell application "Music"
    set results to tracks of library playlist 1 whose id is not missing value
    if results is not {{}} then
        return persistent ID of (item (count of results) of results)
    end if
    return ""
end tell
"""
    return run_applescript(find_script) or None


def find_and_add_track(name: str, artist: str) -> str | None:
    script = f"""
tell application "Music"
    set results to search library playlist 1 for "{escape(artist)} {escape(name)}"
    if results is {{}} then
        set results to search library playlist 1 for "{escape(name)}"
    end if
    if results is {{}} then
        return ""
    end if
    set bestTrack to missing value
    repeat with t in results
        if (name of t as string) is "{escape(name)}" and (artist of t as string) is "{escape(artist)}" then
            set bestTrack to t
            exit repeat
        end if
    end repeat
    if bestTrack is missing value then set bestTrack to item 1 of results
    try
        add bestTrack to library playlist 1
    end try
    return persistent ID of bestTrack
end tell
"""
    result = run_applescript(script, timeout=15)
    return result if result else None


def set_loved(persistent_id: str) -> None:
    script = f"""
tell application "Music"
    set results to (tracks of library playlist 1 whose persistent ID is "{persistent_id}")
    if results is not {{}} then
        set loved of (item 1 of results) to true
    end if
end tell
"""
    run_applescript(script)


def ensure_playlist(name: str) -> None:
    script = f"""
tell application "Music"
    if not (exists playlist "{escape(name)}") then
        make new playlist with properties {{name:"{escape(name)}"}}
    end if
end tell
"""
    run_applescript(script)


def add_to_playlist(persistent_id: str, playlist_name: str) -> None:
    script = f"""
tell application "Music"
    set results to (tracks of library playlist 1 whose persistent ID is "{persistent_id}")
    if results is not {{}} then
        set t to item 1 of results
        if not (exists playlist "{escape(playlist_name)}") then
            make new playlist with properties {{name:"{escape(playlist_name)}"}}
        end if
        try
            duplicate t to playlist "{escape(playlist_name)}"
        end try
    end if
end tell
"""
    run_applescript(script)


def restore_to_favourite_songs(persistent_id: str) -> bool:
    script = f"""
tell application "Music"
    set results to (tracks of library playlist 1 whose persistent ID is "{persistent_id}")
    if results is {{}} then return "not_found"
    set t to item 1 of results
    if not (exists playlist "Favourite Songs") then
        make new playlist with properties {{name:"Favourite Songs"}}
    end if
    set favTracks to (tracks of playlist "Favourite Songs" whose persistent ID is "{persistent_id}")
    if favTracks is {{}} then
        duplicate t to playlist "Favourite Songs"
    end if
    return "ok"
end tell
"""
    result = run_applescript(script)
    return result == "ok"


def restore(backup_path: Path, dry_run: bool, skip_playlists: bool) -> None:
    if not backup_path.exists():
        sys.exit(f"Backup file not found: {backup_path}")

    payload = json.loads(backup_path.read_text())
    tracks = payload["tracks"]
    created_at = payload.get("created_at", "unknown")

    print(f"[restore] backup from {created_at}")
    print(f"[restore] {len(tracks)} tracks, {payload.get('favourite_count',0)} favourites")

    if dry_run:
        print("[restore] DRY RUN — nothing will be modified\n")

    all_playlists = {p for t in tracks for p in t.get("playlists", [])}
    if not skip_playlists and not dry_run:
        for pname in sorted(all_playlists):
            ensure_playlist(pname)
        print(f"[restore] {len(all_playlists)} playlists ensured", flush=True)

    added = skipped = loved_set = playlist_added = 0

    loved_tracks = [t for t in tracks if t.get("loved")]
    other_tracks = [t for t in tracks if not t.get("loved")]

    print(f"\n[restore] restoring {len(loved_tracks)} favourite songs…", flush=True)
    for i, track in enumerate(loved_tracks, 1):
        name = track["name"]
        artist = track["artist"]
        pid = track["persistent_id"]
        playlists = track.get("playlists", [])

        print(f"[restore] [{i}/{len(loved_tracks)}] {artist} — {name}", flush=True)

        if dry_run:
            loved_set += 1
            playlist_added += len(playlists)
            continue

        ok = restore_to_favourite_songs(pid)
        if ok:
            loved_set += 1
        else:
            apple_id = track.get("apple_id", "")
            new_pid = None
            if apple_id:
                new_pid = add_track_by_catalog_id(apple_id)
            if not new_pid:
                new_pid = find_and_add_track(name, artist)
            if new_pid:
                restore_to_favourite_songs(new_pid)
                loved_set += 1
                pid = new_pid
            else:
                print(f"  not found, skipping", flush=True)
                skipped += 1
                continue

        added += 1

        if not skip_playlists:
            for pname in playlists:
                add_to_playlist(pid, pname)
                playlist_added += 1

        time.sleep(0.05)

    if other_tracks:
        print(f"\n[restore] restoring {len(other_tracks)} non-favourite library tracks…", flush=True)
        for i, track in enumerate(other_tracks, 1):
            name = track["name"]
            artist = track["artist"]
            playlists = track.get("playlists", [])

            print(f"[restore] [{i}/{len(other_tracks)}] {artist} — {name}", flush=True)

            if dry_run:
                added += 1
                playlist_added += len(playlists)
                continue

            apple_id = track.get("apple_id", "")
            pid = None
            if apple_id:
                pid = add_track_by_catalog_id(apple_id)
            if not pid:
                pid = find_and_add_track(name, artist)
            if not pid:
                print(f"  not found in catalog, skipping", flush=True)
                skipped += 1
                continue

            added += 1

            if not skip_playlists:
                for pname in playlists:
                    add_to_playlist(pid, pname)
                    playlist_added += 1

            time.sleep(0.1)

    print(f"\n[restore] done.")
    print(f"  added:          {added}")
    print(f"  skipped:        {skipped}")
    print(f"  loved restored: {loved_set}")
    if not skip_playlists:
        print(f"  playlist adds:  {playlist_added}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", type=Path, required=True,
                    help="path to backup JSON from backup_apple_music.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be restored without changing anything")
    ap.add_argument("--skip-playlists", action="store_true",
                    help="restore library + loved only, skip playlist memberships")
    args = ap.parse_args()
    restore(args.backup, dry_run=args.dry_run, skip_playlists=args.skip_playlists)


if __name__ == "__main__":
    main()
