"""Console rendering for Pass 1 / Pass 2 import reports."""

from typing import Dict


def fmt_ms(ms: int) -> str:
    """Format milliseconds as M:SS.s."""
    s = ms / 1000.0
    m = int(s // 60)
    s -= m * 60
    return f"{m}:{s:04.1f}"


# Backwards-compat alias for tests that imported _fmt_ms
_fmt_ms = fmt_ms


def _print_track_entry(entry: Dict, indent: str = "  ") -> None:
    effects = ""
    if entry.get("effects_out") or entry.get("effects_in"):
        parts = []
        if entry.get("effects_out"):
            parts.append(f"out: {', '.join(entry['effects_out'])}")
        if entry.get("effects_in"):
            parts.append(f"in: {', '.join(entry['effects_in'])}")
        effects = f"  [{'; '.join(parts)}]"
    suffix = f" (BP:{entry['beatport_id']})" if "beatport_id" in entry else ""
    print(f"{indent}{entry['position']:2}. {entry['artist']} - {entry['title']}{suffix}{effects}")

    cues = entry.get("cues", [])
    if cues:
        cue_strs = [f"{c['letter']}={fmt_ms(c['ms'])}({c['label']})" for c in cues]
        print(f"{indent}    Cues: {', '.join(cue_strs)}")


def print_report(report: Dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"\n{'=' * 70}")
    print(f"{prefix}Import Report: {report['mix_name']}")
    print(f"{'=' * 70}")

    matched = len(report["matched"])
    created = len(report["created"])
    unmatched = len(report["unmatched"])

    print(
        f"\nTracks: {report['total_tracks']} total, "
        f"{matched} found in DB, {created} created, {unmatched} skipped"
    )

    if report["matched"]:
        print(f"\nAlready in rekordbox:")
        for m in report["matched"]:
            _print_track_entry(m)

    if report["created"]:
        print(f"\nCreated in rekordbox:")
        for c in report["created"]:
            _print_track_entry(c)

    if report["unmatched"]:
        print(f"\nSkipped (no Beatport ID):")
        for u in report["unmatched"]:
            bp = f" (Beatport: {u['beatport_id']})" if u.get("beatport_id") else ""
            print(f"  {u['position']:2}. {u['artist']} - {u['title']}{bp}")

    if not dry_run:
        if report["playlist_created"]:
            print(f"\nPlaylist '{report['mix_name']}' created.")
        if report["created"]:
            print(f"{created} new tracks added to rekordbox collection.")
        print(f"Effects written to {report['effects_written']} tracks.")
        print(f"\nNext steps:")
        print(f"  1. Open rekordbox and let it analyze all tracks in the playlist")
        print(f"  2. Once analysis is complete, close rekordbox and run:")
        print(f"     uv run dj_cli.py migrate {report['mix_name']}.json --pass2-only")
    else:
        if unmatched > 0 and any(u.get("beatport_id") for u in report["unmatched"]):
            print(f"\n{unmatched} tracks will be created in rekordbox on actual run.")
        print(f"\nNo changes made (dry run).")

    print(f"{'=' * 70}\n")


def print_cues_report(report: Dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"\n{'=' * 70}")
    print(f"{prefix}Cues Report: {report['mix_name']}")
    print(f"{'=' * 70}")

    found = len(report["found"])
    not_found = len(report["not_found"])
    snapped_count = sum(1 for t in report["found"] if t["snapped"])
    unsnapped_count = found - snapped_count

    print(
        f"\nTracks: {report['total_tracks']} total, "
        f"{found} found in DB, {not_found} not found"
    )

    if report["found"]:
        print(f"\nCue points:")
        for entry in report["found"]:
            snap_tag = "[SNAPPED]" if entry["snapped"] else "[unsnapped]"
            suffix = f" (BP:{entry['beatport_id']})" if entry.get("beatport_id") else ""
            print(f"  {entry['position']:2}. {entry['artist']} - {entry['title']}{suffix}  {snap_tag}")
            cues = entry.get("cues", [])
            if cues:
                cue_strs = [f"{c['letter']}={fmt_ms(c['ms'])}({c['label']})" for c in cues]
                print(f"      Cues: {', '.join(cue_strs)}")

    if report["not_found"]:
        print(f"\nNot found in DB (run Pass 1 first):")
        for entry in report["not_found"]:
            bp = f" (BP:{entry['beatport_id']})" if entry.get("beatport_id") else ""
            print(f"  {entry['position']:2}. {entry['artist']} - {entry['title']}{bp}")

    if report["no_beatgrid"]:
        print(f"\nNo beatgrid (analyze in rekordbox first):")
        for title in report["no_beatgrid"]:
            print(f"  - {title}")

    print(f"\nSummary: {snapped_count} snapped, {unsnapped_count} unsnapped")
    if not dry_run:
        print(f"Hot cues written to {report['cues_written']} tracks.")
    else:
        print(f"\nNo changes made (dry run).")
    print(f"{'=' * 70}\n")
