"""Beatport genre → destination playlist classifier."""
from __future__ import annotations

import re
from typing import Optional

EXACT: dict[str, str] = {
    "dance/pop": "Dance",
    "melodic house & techno": "Melodic House",
    "house": "House",
    "tech house": "Tech House",
    "indie dance": "Indie Dance",
    "drum & bass": "DnB",
    "techno (raw / deep / hypnotic)": "Hypnotic Techno",
    "techno (peak time / driving)": "Peak Techno",
    "downtempo": "Downtempo",
    "progressive house": "Progressive House",
    "bass house": "Bass House",
    "afro house": "Afro House",
    "deep house": "Deep House",
    "hard techno": "Hard Techno",
    "ambient": "Ambient",
    "electronica": "Electronica",
}

CONTAINS: list[tuple[str, str]] = [
    ("dubstep", "Dubstep"),
    ("mainstage", "Mainstage"),
    ("minimal", "Minimal"),
    ("trance", "Trance"),
]

DESTINATION_PLAYLISTS: set[str] = set(EXACT.values()) | {dest for _, dest in CONTAINS}


def _normalize_genre(g: str) -> str:
    g = g.strip().lower()
    g = re.sub(r"\s*/\s*", "/", g)
    g = re.sub(r"\s+", " ", g)
    return g


def classify(genre: Optional[str]) -> Optional[str]:
    """Map a Beatport genre string to a destination playlist name, or None."""
    if not genre:
        return None
    g = _normalize_genre(genre)
    if g in EXACT:
        return EXACT[g]
    for needle, dest in CONTAINS:
        if needle in g:
            return dest
    return None
