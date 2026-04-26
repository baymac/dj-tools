#!/usr/bin/env python3
"""
Full-pipeline DJ import wrapper.

Runs the complete import flow in one command, watching the rekordbox analysis
directory so it can automatically proceed to Pass 2 once all tracks are analyzed.

Usage:
    uv run dj_import.py "Mix Name"          # extract JSON → Pass 1 → watch → Pass 2
    uv run dj_import.py mix.json            # skip extraction, use existing JSON
    uv run dj_import.py --list              # list available mixes
    uv run dj_import.py mix.json --no-watch # Pass 1 only (skip analysis wait)
    uv run dj_import.py mix.json --pass2-only  # just watch + Pass 2
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent

# rekordbox writes analysis (.DAT/.EXT) files here for streaming tracks
RB6_SHARE = (
    Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox6" / "share"
)


def rekordbox_running() -> bool:
    return subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True).returncode == 0


def count_anlz_files() -> int:
    """Count .DAT files under the rekordbox6 share folder as an analysis proxy."""
    if not RB6_SHARE.exists():
        return 0
    return sum(1 for _ in RB6_SHARE.rglob("*.DAT"))


def run_script(args: list) -> int:
    """Run a uv script, streaming its output. Returns the exit code."""
    return subprocess.run(["uv", "run"] + [str(a) for a in args]).returncode


def watch_for_analysis(n_tracks: int):
    """Block until rekordbox has opened, analyzed the playlist, and closed.

    Tracks progress by watching new .DAT files appear in the rekordbox share
    folder (each analyzed track creates ~2 files: .DAT + .EXT). Prints a
    running count and prompts the user to close rekordbox once done.
    """
    baseline = count_anlz_files()

    print(f"\nOpen rekordbox, select the playlist, and let it analyze {n_tracks} tracks.")
    print("Waiting for rekordbox...")

    while not rekordbox_running():
        time.sleep(2)
    print("Rekordbox detected. Watching analysis progress...\n")

    last_count = baseline
    stable_since = time.time()
    done_announced = False

    while rekordbox_running():
        current = count_anlz_files()
        new_files = current - baseline

        if current != last_count:
            stable_since = time.time()
            last_count = current

        idle = int(time.time() - stable_since)
        # Each track generates a .DAT + .EXT, so ~2 files per track
        estimated = min(new_files // 2, n_tracks)
        bar = "#" * estimated + "-" * (n_tracks - estimated)
        print(f"\r  [{bar}] {estimated}/{n_tracks}  ({new_files} new files, idle {idle}s)  ", end="", flush=True)

        if not done_announced and estimated >= n_tracks and idle >= 10:
            print(f"\n\nAll tracks analyzed. Close rekordbox to continue.")
            done_announced = True

        time.sleep(3)

    print(f"\n\nRekordbox closed.")


def main():
    parser = argparse.ArgumentParser(
        description="Full DJ Studio → rekordbox import pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run dj_import.py "My Mix"            Full pipeline: extract + pass1 + watch + pass2
  uv run dj_import.py mix.json            Use existing JSON (skip extraction)
  uv run dj_import.py mix.json --dry-run  Preview Pass 1 without writing
  uv run dj_import.py mix.json --no-watch Pass 1 only, skip analysis wait
  uv run dj_import.py mix.json --pass2-only  Watch + Pass 2 only (Pass 1 already done)
        """,
    )
    parser.add_argument("target", nargs="?", help="Mix name or path to .json file")
    parser.add_argument("--list", action="store_true", help="List available mixes")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--no-watch", action="store_true", help="Run Pass 1 only, skip the analysis watch")
    parser.add_argument("--pass2-only", action="store_true", help="Skip Pass 1, just watch + Pass 2")
    parser.add_argument("--no-snap", action="store_true", help="Pass 2: skip beatgrid snapping")
    args = parser.parse_args()

    if args.list:
        sys.exit(run_script([SCRIPTS / "get_mix_info.py", "--list"]))

    if not args.target:
        parser.print_help()
        sys.exit(0)

    target = args.target

    # Resolve JSON path
    if target.endswith(".json"):
        json_path = Path(target)
        if not json_path.exists():
            print(f"Error: {json_path} not found.", file=sys.stderr)
            sys.exit(1)
        mix_name = json_path.stem
    else:
        mix_name = target
        json_path = Path(f"{mix_name}.json")

    # ── Step 1: Extract mix JSON ──────────────────────────────────────────────
    if not target.endswith(".json") and not args.pass2_only:
        print(f"\n{'='*60}")
        print(f"Step 1/3  Extract — '{mix_name}'")
        print(f"{'='*60}")
        rc = run_script([SCRIPTS / "get_mix_info.py", mix_name, "-o", str(json_path)])
        if rc != 0:
            sys.exit(rc)

    # Load JSON to get track count for the progress bar
    if not json_path.exists():
        print(f"Error: {json_path} not found. Run Pass 1 first.", file=sys.stderr)
        sys.exit(1)
    with open(json_path) as f:
        mix_data = json.load(f)
    n_tracks = len(mix_data.get("tracks", []))

    # ── Step 2: Pass 1 — create tracks, playlist, effects ────────────────────
    if not args.pass2_only:
        label = "2/3" if not target.endswith(".json") else "1/2"
        print(f"\n{'='*60}")
        print(f"Step {label}  Pass 1 — import tracks and playlist")
        print(f"{'='*60}")
        cmd = [SCRIPTS / "import_to_rekordbox.py", json_path]
        if args.dry_run:
            cmd.append("--dry-run")
        rc = run_script(cmd)
        if rc != 0:
            sys.exit(rc)
        if args.dry_run or args.no_watch:
            sys.exit(0)

    # ── Step 3: Watch for analysis → Pass 2 ──────────────────────────────────
    label = "3/3" if not target.endswith(".json") else "2/2"
    print(f"\n{'='*60}")
    print(f"Step {label}  Analysis watch + Pass 2 ({n_tracks} tracks)")
    print(f"{'='*60}")
    watch_for_analysis(n_tracks)

    print("\nRunning Pass 2 — writing cues...")
    cmd = [SCRIPTS / "import_to_rekordbox.py", json_path, "--cues-only"]
    if args.no_snap:
        cmd.append("--no-snap")
    sys.exit(run_script(cmd))


if __name__ == "__main__":
    main()
