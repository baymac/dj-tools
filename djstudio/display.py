"""Console rendering for DJ Studio mix data."""

from typing import Dict, List


def print_mix_info(mix_info: Dict) -> None:
    """Pretty-print full mix details: metadata, tracks, transitions."""
    meta = mix_info["metadata"]

    print("=" * 100)
    print(f"{meta['name'].upper():^100}")
    print("=" * 100)
    print(
        f"Duration: {meta['duration_formatted']} | "
        f"Tracks: {meta['track_count']} | "
        f"BPM: {meta['bpm_min']}-{meta['bpm_max']}"
    )
    print(
        f"Genre: {meta['genre']} | "
        f"Created: {meta['created'][:10]} | "
        f"Modified: {meta['last_modified'][:10]}"
    )
    print("=" * 100)
    print()

    print("TRACKLIST:")
    print("-" * 100)
    for track in mix_info["tracks"]:
        print(f"{track['position']:2}. {track['artist']} - {track['title']}")
        print(
            f"    BPM: {track['bpm']} | Key: {track['key']} | "
            f"Length: {track['duration_formatted']} | "
            f"Beats: {track.get('start_beat', '?')}-{track.get('end_beat', '?')}"
        )
    print()

    print(f"TRANSITIONS ({len(mix_info['transitions'])} total):")
    print("-" * 100)
    for trans in mix_info["transitions"]:
        effects_str = ", ".join(trans["effects"])
        n = trans["number"]
        print(
            f"  {n:2} -> {n+1:2}. Duration: {trans['duration_beats']} beats | "
            f"Type: {trans['transition_type']} | "
            f"Offset: {trans.get('effect_offset', 0)}"
        )
        print(f"          Effects: {effects_str}")
    print()
    print("=" * 100)


def print_mix_list(projects: List[Dict]) -> None:
    """Print the table of all mixes from get_all_projects()."""
    print(f"\nFound {len(projects)} mixes:\n")
    print(f"{'Name':<40} {'Genre':<15} {'Tracks':<8} {'Duration':<10} {'Modified'}")
    print("-" * 100)
    for project in projects:
        print(
            f"{project['name']:<40} "
            f"{project['genre']:<15} "
            f"{project['tracks']:<8} "
            f"{project['duration']:>6.1f}m   "
            f"{project['modified'][:10]}"
        )
