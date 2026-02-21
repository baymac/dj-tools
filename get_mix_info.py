#!/usr/bin/env python3
"""
DJ Studio Mix Information Extractor

This script extracts complete mix information from DJ Studio's local database.
It can list all mixes, show detailed information for a specific mix, and export
mix metadata to JSON format.

Usage:
    python3 get_mix_info.py --list                    # List all mixes
    python3 get_mix_info.py "Mix Name"                # Show mix details
    python3 get_mix_info.py "Mix Name" --json         # Output as JSON
    python3 get_mix_info.py "Mix Name" -o file.json   # Export to file

Author: Generated for DJ Studio data extraction
Date: 2026-02-20
"""

import json
import os
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Optional


class DJStudioMixExtractor:
    """Extract and format DJ Studio mix information."""

    def __init__(self):
        self.home = Path.home()
        self.db_path = self.home / "Music" / "DJ.Studio" / "Database"
        self.config_path = self.home / "Library" / "Application Support" / "DJ.Studio" / "config.json"
        self.library_dir = self.db_path / "audio-library-table"

        # Load audio library from sharded directory structure:
        # audio-library-table/{hash_prefix}/{library_key}
        self.audio_library = {}
        if self.library_dir.is_dir():
            for shard in self.library_dir.iterdir():
                if not shard.is_dir():
                    continue
                for entry in shard.iterdir():
                    if not entry.is_file():
                        continue
                    try:
                        with open(entry, 'r') as f:
                            track = json.load(f)
                            self.audio_library[track['key']] = track
                    except Exception:
                        continue

    def get_all_projects(self) -> List[Dict]:
        """Get list of all mix projects."""
        projects_dir = self.db_path / "projects-table"
        projects = []

        for project_file in projects_dir.glob("*"):
            if project_file.is_file():
                try:
                    with open(project_file, 'r') as f:
                        project = json.load(f)
                        projects.append({
                            'uuid': project['key'],
                            'name': project['name'],
                            'genre': project.get('genre', ''),
                            'tracks': project.get('trackCount', 0),
                            'duration': project.get('duration', 0) / 60,  # Convert to minutes
                            'created': project.get('createdAt', ''),
                            'modified': project.get('lastModified', '')
                        })
                except Exception as e:
                    print(f"Error reading {project_file}: {e}", file=sys.stderr)

        return sorted(projects, key=lambda x: x['modified'], reverse=True)

    def find_project_by_name(self, name: str) -> Optional[str]:
        """Find project UUID by mix name (case-insensitive)."""
        projects_dir = self.db_path / "projects-table"
        name_lower = name.lower()

        for project_file in projects_dir.glob("*"):
            if project_file.is_file():
                try:
                    with open(project_file, 'r') as f:
                        project = json.load(f)
                        if project['name'].lower() == name_lower:
                            return project['key']
                except Exception:
                    continue

        return None

    def get_mix_info(self, project_uuid: str) -> Optional[Dict]:
        """Get complete mix information by UUID."""
        project_file = self.db_path / "projects-table" / project_uuid

        if not project_file.exists():
            return None

        with open(project_file, 'r') as f:
            project = json.load(f)

        project_key = project['key']

        # Build mixTrackKey -> track position lookup
        mix_key_to_pos = {}
        for i, track_ref in enumerate(project['mixList']):
            mix_key_to_pos[track_ref['key']] = i  # 0-indexed

        # Extract track information
        tracks = []
        for i, track_ref in enumerate(project['mixList'], 1):
            library_key = track_ref['libraryKey']
            track_info = self.audio_library.get(library_key, {})
            tag = track_info.get('tag', {})

            # Get start/end beats from systemCuePoints
            cue_data = track_info.get('cueData', {})
            sys_cues = cue_data.get('systemCuePoints', [])
            start_beat = sys_cues[0]['beat'] if len(sys_cues) > 0 else 0
            end_beat = sys_cues[1]['beat'] if len(sys_cues) > 1 else 0

            tracks.append({
                'position': i,
                'title': tag.get('title', 'Unknown'),
                'artist': tag.get('artist', 'Unknown'),
                'bpm': track_info.get('bpm', 'N/A'),
                'key': self._get_camelot_key(track_info.get('camelotKey')),
                'duration': track_info.get('duration', 0),
                'duration_formatted': self._format_duration(track_info.get('duration', 0)),
                'genre': tag.get('genre', ''),
                'library_key': library_key,
                'start_beat': start_beat,
                'end_beat': end_beat,
            })

        # Extract transition information, keyed by track position
        # Each autoEffect has a mixTrackKey pointing to the outgoing track
        transitions = []
        for effect in project['autoEffects']:
            mix_track_key = effect.get('mixTrackKey', '')
            track_pos = mix_key_to_pos.get(mix_track_key)
            if track_pos is None:
                continue
            # number = 1-indexed position of the outgoing track
            transitions.append({
                'number': track_pos + 1,
                'duration_beats': effect['duration'],
                'effects': [e['effectName'] for e in effect['effects']],
                'effect_offset': effect.get('effectOffset', 0),
                'transition_type': effect.get('transitionType', 'N/A'),
                'is_manual': effect.get('manual', False)
            })

        # Sort transitions by track position
        transitions.sort(key=lambda t: t['number'])

        return {
            'metadata': {
                'uuid': project['key'],
                'name': project['name'],
                'genre': project.get('genre', ''),
                'duration_seconds': project['duration'],
                'duration_minutes': round(project['duration'] / 60, 2),
                'duration_formatted': self._format_duration(project['duration']),
                'track_count': project['trackCount'],
                'bpm_min': round(project['minBpm'], 1),
                'bpm_max': round(project['maxBpm'], 1),
                'created': project['createdAt'],
                'last_modified': project['lastModified']
            },
            'tracks': tracks,
            'transitions': transitions
        }

    def _get_camelot_key(self, key_num: Optional[int]) -> str:
        """Convert numeric key to Camelot notation."""
        if key_num is None or key_num == 'N/A':
            return 'N/A'

        camelot_map = {
            1: '8B', 2: '3B', 3: '10B', 4: '5B', 5: '12B', 6: '7B',
            7: '2B', 8: '9B', 9: '4B', 10: '11B', 11: '6B', 12: '1B',
            13: '5A', 14: '12A', 15: '7A', 16: '2A', 17: '9A', 18: '4A',
            19: '11A', 20: '6A', 21: '1A', 22: '8A', 23: '3A', 24: '10A'
        }

        return camelot_map.get(key_num, str(key_num))

    def _format_duration(self, seconds: float) -> str:
        """Format duration in MM:SS format."""
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"

    def print_mix_info(self, mix_info: Dict):
        """Print formatted mix information to console."""
        meta = mix_info['metadata']

        print('=' * 100)
        print(f"{meta['name'].upper():^100}")
        print('=' * 100)
        print(f"Duration: {meta['duration_formatted']} | "
              f"Tracks: {meta['track_count']} | "
              f"BPM: {meta['bpm_min']}-{meta['bpm_max']}")
        print(f"Genre: {meta['genre']} | "
              f"Created: {meta['created'][:10]} | "
              f"Modified: {meta['last_modified'][:10]}")
        print('=' * 100)
        print()

        print("TRACKLIST:")
        print("-" * 100)
        for track in mix_info['tracks']:
            print(f"{track['position']:2}. {track['artist']} - {track['title']}")
            print(f"    BPM: {track['bpm']} | Key: {track['key']} | "
                  f"Length: {track['duration_formatted']} | "
                  f"Beats: {track.get('start_beat', '?')}-{track.get('end_beat', '?')}")
        print()

        print(f"TRANSITIONS ({len(mix_info['transitions'])} total):")
        print("-" * 100)
        for trans in mix_info['transitions']:
            effects_str = ', '.join(trans['effects'])
            n = trans['number']
            print(f"  {n:2} -> {n+1:2}. Duration: {trans['duration_beats']} beats | "
                  f"Type: {trans['transition_type']} | "
                  f"Offset: {trans.get('effect_offset', 0)}")
            print(f"          Effects: {effects_str}")
        print()
        print('=' * 100)


def main():
    parser = argparse.ArgumentParser(
        description='Extract DJ Studio mix information',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list                    List all mixes
  %(prog)s "Ibiza Vibes"             Show mix details
  %(prog)s "Ibiza Vibes" --json      Output as JSON
  %(prog)s "Ibiza Vibes" -o mix.json Export to file
        """
    )

    parser.add_argument('mix_name', nargs='?', help='Name of the mix to extract')
    parser.add_argument('--list', '-l', action='store_true', help='List all available mixes')
    parser.add_argument('--json', '-j', action='store_true', help='Output as JSON')
    parser.add_argument('--output', '-o', help='Output file path (JSON format)')

    args = parser.parse_args()

    extractor = DJStudioMixExtractor()

    # List all mixes
    if args.list:
        projects = extractor.get_all_projects()
        print(f"\nFound {len(projects)} mixes:\n")
        print(f"{'Name':<40} {'Genre':<15} {'Tracks':<8} {'Duration':<10} {'Modified'}")
        print("-" * 100)
        for project in projects:
            print(f"{project['name']:<40} "
                  f"{project['genre']:<15} "
                  f"{project['tracks']:<8} "
                  f"{project['duration']:>6.1f}m   "
                  f"{project['modified'][:10]}")
        return

    # Extract specific mix
    if not args.mix_name:
        parser.print_help()
        return

    # Find project
    project_uuid = extractor.find_project_by_name(args.mix_name)
    if not project_uuid:
        print(f"Error: Mix '{args.mix_name}' not found.", file=sys.stderr)
        print("\nAvailable mixes:", file=sys.stderr)
        for project in extractor.get_all_projects():
            print(f"  - {project['name']}", file=sys.stderr)
        sys.exit(1)

    # Get mix info
    mix_info = extractor.get_mix_info(project_uuid)
    if not mix_info:
        print(f"Error: Could not load mix data.", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(mix_info, f, indent=2)
        print(f"Mix information saved to: {args.output}")
    elif args.json:
        print(json.dumps(mix_info, indent=2))
    else:
        extractor.print_mix_info(mix_info)


if __name__ == '__main__':
    main()
