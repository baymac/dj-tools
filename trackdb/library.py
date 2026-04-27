"""DJ Studio library / structures / stems readers used to seed the track DB."""

import json
import re
import struct
from pathlib import Path
from typing import Optional, Set, Tuple

from djstudio.extractor import find_latest_project

from .schema import CAMELOT_MAP


_DJ_DB = Path.home() / "Music" / "DJ.Studio" / "Database"
DJ_STUDIO_LIBRARY = _DJ_DB / "audio-library-table"
DJ_STUDIO_PROJECTS = _DJ_DB / "projects-table"
DJ_STUDIO_STRUCTURES = _DJ_DB / "track-structures-table"
DJ_STUDIO_STEMS = {
    "vocals": _DJ_DB / "audio-library-compressedAudioViewVocals",
    "drums":  _DJ_DB / "audio-library-compressedAudioViewDrums",
    "melody": _DJ_DB / "audio-library-compressedAudioViewMelody",
}


def mix_track_keys(mix_name: str) -> Tuple[Set[str], Optional[dict]]:
    """Return (library_keys, project) for the latest project matching this name.

    Returns (empty set, None) if the name doesn't match any DJ Studio project.
    """
    project = find_latest_project(mix_name, DJ_STUDIO_PROJECTS)
    if project is None:
        return set(), None
    keys = {
        ref.get("libraryKey")
        for ref in project.get("mixList", [])
        if ref.get("libraryKey")
    }
    return keys, project


def load_dj_studio_library() -> dict:
    library: dict = {}
    if not DJ_STUDIO_LIBRARY.is_dir():
        return library
    for shard in DJ_STUDIO_LIBRARY.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if not entry.is_file():
                continue
            try:
                with open(entry) as f:
                    track = json.load(f)
                    library[track["key"]] = track
            except Exception as e:
                print(f"Warning: could not read library shard {entry.name}: {e}", file=__import__("sys").stderr)
                continue
    return library


def load_dj_studio_structures() -> dict:
    """Load track-structures-table, keyed by structureKey (== library_key)."""
    structures: dict = {}
    if not DJ_STUDIO_STRUCTURES.is_dir():
        return structures
    for shard in DJ_STUDIO_STRUCTURES.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if not entry.is_file():
                continue
            try:
                with open(entry) as f:
                    data = json.load(f)
                    structures[data["key"]] = data
            except Exception:
                continue
    return structures


def camelot_key(key_num) -> Optional[str]:
    if key_num is None:
        return None
    try:
        return CAMELOT_MAP.get(int(key_num))
    except (TypeError, ValueError):
        return None


def beatport_url(beatport_id: str, title: str) -> str:
    """Beatport canonical URL: https://www.beatport.com/track/{slug}/{id}."""
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"https://www.beatport.com/track/{slug}/{beatport_id}"


def _find_shard_file(base_dir: Path, library_key: str) -> Optional[Path]:
    if not base_dir.is_dir():
        return None
    for shard in base_dir.iterdir():
        if not shard.is_dir():
            continue
        candidate = shard / library_key
        if candidate.is_file():
            return candidate
    return None


def _stem_energy_ratio(path: Path) -> Optional[float]:
    """compressedAudioView format: 2-byte 0xffff sentinel + 8-byte records.

    Each record = 4 uint16 LE values; field[3] is the energy amplitude.
    Returns mean energy as a 0-1 ratio.
    """
    try:
        data = path.read_bytes()
        payload = data[2:]
        n = len(payload) // 8
        if n == 0:
            return None
        total = sum(struct.unpack_from("<H", payload, i * 8 + 6)[0] for i in range(n))
        return (total / n) / 65535.0
    except Exception:
        return None


# Calibrated against 18 tracks: vocals 0-0.16, drums 0.04-0.42, melody 0-0.52
def ratio_to_intensity(ratio: float) -> str:
    if ratio < 0.03:
        return "none"
    if ratio < 0.10:
        return "low"
    if ratio < 0.25:
        return "medium"
    return "high"


def read_stem_intensities(library_key: str) -> dict:
    """vocals/drums/melody → 'none'|'low'|'medium'|'high', or {} if no stem data."""
    result = {}
    for stem, base_dir in DJ_STUDIO_STEMS.items():
        path = _find_shard_file(base_dir, library_key)
        if path is None:
            continue
        ratio = _stem_energy_ratio(path)
        if ratio is not None:
            result[stem] = ratio_to_intensity(ratio)
    return result


def infer_section_type(label: int, nr: int, total: int) -> str:
    """Map a MIK energy label (1-10) + position to a section type."""
    if nr == 0:
        return "intro"
    if nr == total - 1:
        return "outro"
    if label >= 8:
        return "drop"
    if label >= 6:
        return "buildup"
    if label <= 4:
        return "breakdown"
    return "verse"
