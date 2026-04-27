"""Extract mix and track data from DJ.Studio's local database."""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .keys import get_camelot_key


class DJStudioMixExtractor:
    """Extract and format DJ Studio mix information."""

    def __init__(self):
        self.home = Path.home()
        self.db_path = self.home / "Music" / "DJ.Studio" / "Database"
        self.config_path = self.home / "Library" / "Application Support" / "DJ.Studio" / "config.json"
        self.library_dir = self.db_path / "audio-library-table"

        # audio-library-table/{hash_prefix}/{library_key}
        self.audio_library: Dict[str, dict] = {}
        if self.library_dir.is_dir():
            for shard in self.library_dir.iterdir():
                if not shard.is_dir():
                    continue
                for entry in shard.iterdir():
                    if not entry.is_file():
                        continue
                    try:
                        with open(entry, "r") as f:
                            track = json.load(f)
                            self.audio_library[track["key"]] = track
                    except Exception:
                        continue

    def get_all_projects(self) -> List[Dict]:
        """Get list of all mix projects, sorted by lastModified desc."""
        projects_dir = self.db_path / "projects-table"
        projects: List[Dict] = []

        for project_file in projects_dir.glob("*"):
            if not project_file.is_file():
                continue
            try:
                with open(project_file, "r") as f:
                    project = json.load(f)
                    projects.append({
                        "uuid": project["key"],
                        "name": project["name"],
                        "genre": project.get("genre", ""),
                        "tracks": project.get("trackCount", 0),
                        "duration": project.get("duration", 0) / 60,
                        "created": project.get("createdAt", ""),
                        "modified": project.get("lastModified", ""),
                    })
            except Exception as e:
                print(f"Error reading {project_file}: {e}", file=sys.stderr)

        return sorted(projects, key=lambda x: x["modified"], reverse=True)

    def find_project_by_name(self, name: str) -> Optional[str]:
        """Return UUID of the latest project matching this name (case-insensitive).

        DJ Studio keeps every saved revision under its own UUID, so the same mix
        name can map to many project files. We pick the one with the most recent
        `lastModified` timestamp.
        """
        project = find_latest_project(name, self.db_path / "projects-table")
        return project["key"] if project else None

    def get_mix_info(self, project_uuid: str) -> Optional[Dict]:
        """Build the mix JSON: metadata + tracks + transitions."""
        project_file = self.db_path / "projects-table" / project_uuid

        if not project_file.exists():
            return None

        with open(project_file, "r") as f:
            project = json.load(f)

        # mixTrackKey -> track position (0-indexed)
        mix_key_to_pos = {ref["key"]: i for i, ref in enumerate(project["mixList"])}

        tracks = []
        for i, track_ref in enumerate(project["mixList"], 1):
            library_key = track_ref["libraryKey"]
            track_info = self.audio_library.get(library_key, {})
            tag = track_info.get("tag", {})

            cue_data = track_info.get("cueData", {})
            sys_cues = cue_data.get("systemCuePoints", [])
            start_beat = sys_cues[0]["beat"] if len(sys_cues) > 0 else 0
            end_beat = sys_cues[1]["beat"] if len(sys_cues) > 1 else 0

            tracks.append({
                "position": i,
                "title": tag.get("title", "Unknown"),
                "artist": tag.get("artist", "Unknown"),
                "bpm": track_info.get("bpm", "N/A"),
                "key": get_camelot_key(track_info.get("camelotKey")),
                "duration": track_info.get("duration", 0),
                "duration_formatted": _format_duration(track_info.get("duration", 0)),
                "genre": tag.get("genre", ""),
                "library_key": library_key,
                "start_beat": start_beat,
                "end_beat": end_beat,
            })

        # Each autoEffect has mixTrackKey pointing to the outgoing track
        transitions = []
        for effect in project["autoEffects"]:
            mix_track_key = effect.get("mixTrackKey", "")
            track_pos = mix_key_to_pos.get(mix_track_key)
            if track_pos is None:
                continue
            transitions.append({
                "number": track_pos + 1,
                "duration_beats": effect["duration"],
                "effects": [e["effectName"] for e in effect["effects"]],
                "effect_offset": effect.get("effectOffset", 0),
                "transition_type": effect.get("transitionType", "N/A"),
                "is_manual": effect.get("manual", False),
            })

        transitions.sort(key=lambda t: t["number"])

        return {
            "metadata": {
                "uuid": project["key"],
                "name": project["name"],
                "genre": project.get("genre", ""),
                "duration_seconds": project["duration"],
                "duration_minutes": round(project["duration"] / 60, 2),
                "duration_formatted": _format_duration(project["duration"]),
                "track_count": project["trackCount"],
                "bpm_min": round(project["minBpm"], 1),
                "bpm_max": round(project["maxBpm"], 1),
                "created": project["createdAt"],
                "last_modified": project["lastModified"],
            },
            "tracks": tracks,
            "transitions": transitions,
        }

    def _get_camelot_key(self, key_num):
        """Backwards-compatible shim for tests; prefer djstudio.keys.get_camelot_key."""
        return get_camelot_key(key_num)

    def _format_duration(self, seconds: float) -> str:
        return _format_duration(seconds)


def _format_duration(seconds: float) -> str:
    """MM:SS."""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def find_latest_project(name: str, projects_dir: Path) -> Optional[Dict]:
    """Walk projects-table for the latest project matching `name`.

    Returns the full project dict (or None). DJ Studio saves a separate file per
    revision, so a single mix name often has many candidates — we keep the one
    with the highest `lastModified`.
    """
    if not projects_dir.is_dir():
        return None

    name_lower = name.lower()
    best: Optional[tuple] = None  # (lastModified, project)

    for pf in projects_dir.glob("*"):
        if not pf.is_file():
            continue
        try:
            with open(pf, "r") as f:
                project = json.load(f)
        except Exception:
            continue
        if project.get("name", "").lower() != name_lower:
            continue
        modified = project.get("lastModified", "")
        if best is None or modified > best[0]:
            best = (modified, project)
    return best[1] if best else None
