"""
Export Apple Music library to CSV in TuneMyMusic-compatible format.

Columns: Track name, Artist name, Album, Playlist name, Type, ISRC, Apple-id
- Playlist name: comma-separated list of playlists the track belongs to
  (always includes "Library Songs")
- Type: "Favorite" if in Favourite Songs playlist, else "Library"
- ISRC: not available via local AppleScript (left blank)
- Apple-id: local database ID (not the iTunes store ID)

Usage:
    uv run python helpers/export_apple_music.py
    uv run python helpers/export_apple_music.py --output my_library.csv
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path.home() / "Music" / "DJ.Studio" / "apple_music_export.csv"


# ---------- AppleScript helpers ----------

def run_applescript(script: str, timeout: int = 300) -> str:
    r = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        sys.exit(f"AppleScript error: {r.stderr.strip()}")
    return r.stdout.strip()


def get_favourite_ids() -> set[str]:
    """Return set of database IDs in the Favourite Songs playlist."""
    script = """
tell application "Music"
    set out to ""
    try
        set fav to playlist "Favourite Songs"
        repeat with t in tracks of fav
            set out to out & (database ID of t as string) & "\\n"
        end repeat
    end try
    return out
end tell
"""
    result = run_applescript(script)
    return {line.strip() for line in result.splitlines() if line.strip()}


def get_playlist_memberships() -> dict[str, list[str]]:
    """Return map of database ID → list of playlist names the track belongs to.
    Skips system/smart playlists and processes user playlists only."""
    script = """
tell application "Music"
    set out to ""
    set skipPlaylists to {"Library", "Music", "Music Videos", "Purchased", "Genius"}
    repeat with p in playlists
        set pname to name of p
        if pname is not in skipPlaylists then
            try
                repeat with t in tracks of p
                    set out to out & (database ID of t as string) & "\\t" & pname & "\\n"
                end repeat
            end try
        end if
    end repeat
    return out
end tell
"""
    print("[export] reading playlist memberships…", flush=True)
    result = run_applescript(script, timeout=600)
    memberships: dict[str, list[str]] = {}
    for line in result.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            did, pname = parts
            memberships.setdefault(did, []).append(pname)
    return memberships


def get_all_tracks() -> list[dict]:
    """Fetch all tracks from the library via AppleScript (tab-separated)."""
    script = r"""
tell application "Music"
    set out to ""
    set allTracks to tracks in library playlist 1
    repeat with t in allTracks
        set tid to database ID of t as string
        set tname to name of t
        set tartist to artist of t
        set talbum to album of t
        set tgenre to genre of t
        set out to out & tid & "	" & tname & "	" & tartist & "	" & talbum & "	" & tgenre & "
"
    end repeat
    return out
end tell
"""
    print("[export] reading full library (this takes ~30-60s for 3500 tracks)…", flush=True)
    result = run_applescript(script, timeout=600)
    tracks = []
    for line in result.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            tracks.append({
                "id": parts[0].strip(),
                "name": parts[1].strip(),
                "artist": parts[2].strip(),
                "album": parts[3].strip(),
                "genre": parts[4].strip() if len(parts) > 4 else "",
            })
    return tracks


# ---------- Export ----------

def export(output_path: Path) -> None:
    print("[export] fetching favourite song IDs…", flush=True)
    fav_ids = get_favourite_ids()
    print(f"[export] {len(fav_ids)} favourite songs found", flush=True)

    tracks = get_all_tracks()
    print(f"[export] {len(tracks)} total library tracks", flush=True)

    memberships = get_playlist_memberships()
    print(f"[export] playlist memberships loaded for {len(memberships)} tracks", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Track name", "Artist name", "Album",
                         "Playlist name", "Type", "ISRC", "Apple-id"])

        for t in tracks:
            tid = t["id"]
            track_playlists = memberships.get(tid, [])
            all_playlists = ["Library Songs"] + [
                p for p in track_playlists if p != "Favourite Songs"
            ]
            playlist_str = ", ".join(dict.fromkeys(all_playlists))

            track_type = "Favorite" if tid in fav_ids else "Library"

            writer.writerow([
                t["name"],
                t["artist"],
                t["album"],
                playlist_str,
                track_type,
                "",
                tid,
            ])

    print(f"[export] wrote {len(tracks)} tracks to {output_path}", flush=True)
    print(f"[export] {sum(1 for t in tracks if t['id'] in fav_ids)} favourites marked", flush=True)


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Export Apple Music library to CSV")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                    help=f"output CSV path (default: {DEFAULT_OUTPUT})")
    args = ap.parse_args()
    export(args.output)


if __name__ == "__main__":
    main()
