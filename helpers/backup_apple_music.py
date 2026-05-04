"""
Backup Apple Music library to a timestamped JSON file using MusicKit.

Uses the Swift MusicKit bridge (sync/bridge/musickit_bridge.swift) to capture the
full iCloud Music Library — all tracks across all devices, not just what's synced
to this Mac.

Backup saved to ~/Documents/apple_music_backups/backup_YYYY-MM-DD_HHMMSS.json

Usage:
    uv run python helpers/backup_apple_music.py
    uv run python helpers/backup_apple_music.py --output /path/to/backup.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWIFT_SCRIPT = ROOT / "connections" / "bridge" / "musickit_bridge.swift"
DEFAULT_DIR = Path.home() / "Documents" / "apple_music_backups"


def run_musickit_export() -> list[dict]:
    print("[backup] running MusicKit export (this takes 2-3 minutes)…", flush=True)
    result = subprocess.run(
        ["swift", str(SWIFT_SCRIPT)],
        capture_output=True, text=True, timeout=600
    )
    for line in result.stderr.splitlines():
        print(f"[backup] {line}", flush=True)

    if result.returncode != 0:
        sys.exit(f"MusicKit export failed:\n{result.stderr}")

    tracks = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tracks.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return tracks


def backup(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tracks = run_musickit_export()
    if not tracks:
        sys.exit("No tracks returned from MusicKit export")

    loved_count = sum(1 for t in tracks if t.get("loved"))
    playlisted_count = sum(1 for t in tracks if t.get("playlists"))
    with_catalog_id = sum(1 for t in tracks if t.get("catalog_id"))

    payload = {
        "created_at": datetime.now().isoformat(),
        "source": "MusicKit",
        "track_count": len(tracks),
        "favourite_count": loved_count,
        "catalog_id_coverage": f"{with_catalog_id}/{len(tracks)}",
        "tracks": tracks,
    }

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    size_kb = output_path.stat().st_size // 1024

    print(f"\n[backup] saved to {output_path} ({size_kb} KB)")
    print(f"[backup]   total tracks:   {len(tracks)}")
    print(f"[backup]   favourites:     {loved_count}")
    print(f"[backup]   playlisted:     {playlisted_count}")
    print(f"[backup]   with Apple ID:  {with_catalog_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=None,
                    help="custom output path (default: ~/Documents/apple_music_backups/backup_TIMESTAMP.json)")
    args = ap.parse_args()

    if args.output:
        out = args.output
    else:
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = DEFAULT_DIR / f"backup_{ts}.json"

    backup(out)


if __name__ == "__main__":
    main()
