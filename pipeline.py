"""Full DJ Studio → Rekordbox pipeline orchestration.

extract → Pass 1 → watch rekordbox analysis → Pass 2 (cues snapped to beatgrid).
"""

import json
import sys
import time
from pathlib import Path
from typing import Optional

from djstudio.extractor import DJStudioMixExtractor
from rekordbox.backup import rekordbox_running
from rekordbox.constants import RB_SHARE
from rekordbox.display import print_cues_report, print_report
from rekordbox.importer import RekordboxImporter


def _load_anlz_sidecar(sidecar_path: Path) -> Optional[list]:
    """Load an ANLZ manifest written by Pass 1. Returns None if not found."""
    if not sidecar_path.exists():
        return None
    try:
        with open(sidecar_path) as f:
            return json.load(f)
    except Exception:
        return None


def watch_for_analysis(n_tracks: int, sidecar_path: Optional[Path] = None) -> None:
    """Block until rekordbox opens, analyzes the playlist, and closes.

    When a sidecar_path is provided (written by Pass 1), progress is tracked by
    checking whether each track's specific .DAT file has appeared. This avoids
    false positives from rekordbox reanalyzing unrelated tracks in the background.

    Falls back to counting all new .DAT files under share/ if no sidecar exists.
    """
    manifest = _load_anlz_sidecar(sidecar_path) if sidecar_path else None
    use_manifest = bool(manifest)

    if use_manifest:
        dat_paths = [
            (entry["title"], RB_SHARE / entry["dat_path"].lstrip("/"))
            for entry in manifest
            if entry.get("dat_path")
        ]
        n_tracks = len(dat_paths)
    else:
        baseline = sum(1 for _ in RB_SHARE.rglob("*.DAT")) if RB_SHARE.exists() else 0

    print(f"\nOpen rekordbox, select the playlist, and let it analyze {n_tracks} tracks.")
    print("Waiting for rekordbox...")

    while not rekordbox_running():
        time.sleep(2)
    print("Rekordbox detected. Watching analysis progress...\n")

    done_announced = False

    if use_manifest:
        last_analyzed = set()

        while rekordbox_running():
            analyzed = {title for title, path in dat_paths if path.exists()}
            pending = [title for title, path in dat_paths if not path.exists()]
            done = len(analyzed)

            if analyzed != last_analyzed:
                last_analyzed = analyzed

            bar = "#" * done + "-" * (n_tracks - done)
            pending_str = (
                f"  waiting: {', '.join(repr(t) for t in pending[:3])}"
                + (" …" if len(pending) > 3 else "")
                if pending else ""
            )
            print(f"\r  [{bar}] {done}/{n_tracks}{pending_str}  ", end="", flush=True)

            if not done_announced and done >= n_tracks:
                print(f"\n\nAll tracks analyzed. Close rekordbox to continue.")
                done_announced = True

            time.sleep(3)
    else:
        last_count = baseline
        stable_since = time.time()

        while rekordbox_running():
            current = sum(1 for _ in RB_SHARE.rglob("*.DAT")) if RB_SHARE.exists() else 0
            new_files = current - baseline

            if current != last_count:
                stable_since = time.time()
                last_count = current

            idle = int(time.time() - stable_since)
            estimated = min(new_files // 2, n_tracks)
            bar = "#" * estimated + "-" * (n_tracks - estimated)
            print(
                f"\r  [{bar}] {estimated}/{n_tracks}  "
                f"({new_files} new files, idle {idle}s)  ",
                end="", flush=True,
            )

            if not done_announced and estimated >= n_tracks and idle >= 10:
                print(f"\n\nAll tracks analyzed. Close rekordbox to continue.")
                done_announced = True

            time.sleep(3)

    print(f"\n\nRekordbox closed.")


def extract_mix(mix_name: str, output_path: Path) -> int:
    """Dump a DJ Studio mix to JSON. Returns exit code."""
    extractor = DJStudioMixExtractor()
    project_uuid = extractor.find_project_by_name(mix_name)
    if not project_uuid:
        print(f"Error: Mix '{mix_name}' not found.", file=sys.stderr)
        print("\nAvailable mixes:", file=sys.stderr)
        for project in extractor.get_all_projects():
            print(f"  - {project['name']}", file=sys.stderr)
        return 1

    mix_info = extractor.get_mix_info(project_uuid)
    if not mix_info:
        print(f"Error: Could not load mix data.", file=sys.stderr)
        return 1

    last_modified = (mix_info["metadata"].get("last_modified") or "?")[:19].replace("T", " ")
    print(f"Using '{mix_info['metadata']['name']}' (modified {last_modified}, uuid {project_uuid[:8]}…)")

    with open(output_path, "w") as f:
        json.dump(mix_info, f, indent=2)
    print(f"Mix information saved to: {output_path}")
    return 0


def run_pass1(json_path: Path, dry_run: bool) -> int:
    with open(json_path) as f:
        json_data = json.load(f)

    print(f"Loaded mix: {json_data['metadata']['name']}")
    print(f"Tracks: {len(json_data['tracks'])}")
    print(f"Transitions: {len(json_data.get('transitions', []))}")
    if dry_run:
        print("\n--- DRY RUN MODE ---")

    importer = RekordboxImporter(dry_run=dry_run, cues_only=False, snap=True)
    try:
        report = importer.import_mix(json_data)
        print_report(report, dry_run)
    finally:
        importer.close()

    if not dry_run:
        manifest = report.get("anlz_manifest", [])
        if manifest:
            sidecar_path = json_path.with_name(json_path.stem + "_anlz.json")
            with open(sidecar_path, "w") as f:
                json.dump(manifest, f, indent=2)
            print(f"ANLZ manifest: {sidecar_path.name} ({len(manifest)} tracks)")

    return 0


def run_pass2(json_path: Path, dry_run: bool, snap: bool) -> int:
    with open(json_path) as f:
        json_data = json.load(f)

    print(f"Loaded mix: {json_data['metadata']['name']}")
    print(f"Tracks: {len(json_data['tracks'])}")
    if dry_run:
        print("\n--- DRY RUN MODE ---")

    importer = RekordboxImporter(dry_run=dry_run, cues_only=True, snap=snap)
    try:
        report = importer.import_cues_only(json_data)
        print_cues_report(report, dry_run)
    finally:
        importer.close()
    return 0


def run_full_pipeline(
    target: str,
    *,
    dry_run: bool = False,
    no_watch: bool = False,
    pass1_only: bool = False,
    pass2_only: bool = False,
    no_snap: bool = False,
    extract_only: bool = False,
    output: Optional[Path] = None,
) -> int:
    """Top-level migrate flow."""
    if target.endswith(".json"):
        json_path = Path(target)
        if not json_path.exists():
            print(f"Error: {json_path} not found.", file=sys.stderr)
            return 1
        mix_name = json_path.stem
    else:
        mix_name = target
        json_path = output if output else Path(f"{mix_name}.json")

    # Step 1: extract
    if not target.endswith(".json") and not pass2_only:
        print(f"\n{'='*60}")
        print(f"Extract — '{mix_name}'")
        print(f"{'='*60}")
        rc = extract_mix(mix_name, json_path)
        if rc != 0 or extract_only:
            return rc

    if not json_path.exists():
        print(f"Error: {json_path} not found. Run extract first.", file=sys.stderr)
        return 1

    with open(json_path) as f:
        mix_data = json.load(f)
    n_tracks = len(mix_data.get("tracks", []))

    # Step 2: Pass 1 (skipped when --pass2-only)
    if not pass2_only:
        print(f"\n{'='*60}")
        print(f"Pass 1 — import tracks and playlist")
        print(f"{'='*60}")
        rc = run_pass1(json_path, dry_run)
        if rc != 0:
            return rc
        if dry_run or no_watch or pass1_only:
            return 0

        # Step 3: watch for rekordbox to analyze, then Pass 2
        print(f"\n{'='*60}")
        print(f"Analysis watch + Pass 2 ({n_tracks} tracks)")
        print(f"{'='*60}")
        sidecar_path = json_path.with_name(json_path.stem + "_anlz.json")
        watch_for_analysis(n_tracks, sidecar_path=sidecar_path)

    # Pass 2 — runs immediately when --pass2-only (analysis already done by user)
    print("\nRunning Pass 2 — writing cues...")
    return run_pass2(json_path, dry_run=dry_run, snap=not no_snap)
