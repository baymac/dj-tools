"""Rekordbox import constants: paths, key/cue mappings, effect sets."""

from pathlib import Path
from typing import Dict, Tuple

from pyrekordbox import config as _rb_config


def _resolve_paths() -> Tuple[Path, Path]:
    """Locate master.db and the share/ folder. Tries rekordbox 7 first, falls back to 6.

    Both versions store the data in different places depending on the install,
    so we ask pyrekordbox rather than hardcoding.
    """
    for version in ("rekordbox7", "rekordbox6"):
        try:
            cfg = _rb_config.get_config(version)
        except Exception:
            cfg = {}
        db_path = cfg.get("db_path")
        db_dir = cfg.get("db_dir")
        if db_path and db_dir:
            return Path(db_path), Path(db_dir) / "share"
    # Fallback to the rekordbox 6 default location
    legacy = Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox6"
    return legacy / "master.db", legacy / "share"


RB_DB_PATH, RB_SHARE = _resolve_paths()
from paths import REKORDBOX_BACKUP_DIR as RB_BACKUP_DIR

# Camelot key string -> DjmdKey ScaleName
# Rekordbox stores keys using its own ScaleName values. For Beatport streaming
# tracks, the key is stored as Camelot notation (e.g., "11A", "6B").
CAMELOT_KEYS = [
    "1A", "2A", "3A", "4A", "5A", "6A", "7A", "8A", "9A", "10A", "11A", "12A",
    "1B", "2B", "3B", "4B", "5B", "6B", "7B", "8B", "9B", "10B", "11B", "12B",
]

# Hot cue letter -> Kind value in DjmdCue (Kind > 0 = hot cue)
CUE_KIND = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8}

# Effects that indicate a bass swap is present in a transition
BASS_SWAP_EFFECTS = {"AE_Bass_Swap", "AE_Bass_SwapFade", "AE_Bass_CrossFade"}

# Effects to write into a track's Commnt field (volume + bass only)
COMMENT_EFFECTS = {
    "AE_CrossFade", "AE_FadeIn", "AE_FadeOut", "AE_Swap",
    "AE_Bass_CrossFade", "AE_Bass_FadeOut", "AE_Bass_Swap", "AE_Bass_SwapFade",
}

# Default bars before transition start; overridden per genre
PREP_BARS = 8

# Genre substring (lowercase) → bars before transition start
GENRE_PREP_BARS: Dict[str, int] = {
    # Techno / industrial: slow-moving, needs a 16-bar runway
    "techno": 16,
    "industrial": 16,
    # Trance: long peak-time builds
    "trance": 16,
    "psytrance": 16,
    # House / afro / melodic: standard 8-bar phrasing
    "house": 8,
    "disco": 8,
    "afro": 8,
    "melodic": 8,
    "electronica": 8,
    # Fast-phrased or bar-dense genres: 4 bars is plenty
    "drum and bass": 4,
    "dnb": 4,
    "jungle": 4,
    "hip hop": 4,
    "hip-hop": 4,
    "trap": 4,
    "r&b": 4,
    "reggaeton": 4,
    "breakbeat": 4,
}
