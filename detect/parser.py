"""Text-based track info extraction from captions and comments."""

from __future__ import annotations

import re


TrackInfo = dict[str, str | int]


def parse_tracks(text: str) -> list[TrackInfo]:
    """
    Try several common tracklist formats and return a list of dicts with keys:
    position, artist (optional), title.
    """
    if not text:
        return []

    text = text.strip()
    tracks: list[TrackInfo] = []

    # Format: "1. Artist - Title" / "1) Artist – Title"
    for m in re.finditer(
        r"(?:^|\n)\s*(\d+)[.)]\s*(.+?)\s*[-–—]\s*(.+?)(?=\n|$)",
        text,
        re.MULTILINE,
    ):
        pos, artist, title = m.groups()
        tracks.append({"position": int(pos), "artist": artist.strip(), "title": title.strip()})

    if tracks:
        return tracks

    # Format: "1. Title only"
    for m in re.finditer(
        r"(?:^|\n)\s*(\d+)[.)]\s*(.+?)(?=\n|$)",
        text,
        re.MULTILINE,
    ):
        pos, title = m.groups()
        t = title.strip()
        if t:
            tracks.append({"position": int(pos), "title": t})

    if tracks:
        return tracks

    # Format: bare "Artist - Title" lines (no numbering)
    for i, m in enumerate(
        re.finditer(r"^(.+?)\s*[-–—]\s*(.+?)$", text, re.MULTILINE), start=1
    ):
        artist, title = m.groups()
        if len(artist) < 80 and len(title) < 120:
            tracks.append({"position": i, "artist": artist.strip(), "title": title.strip()})

    return tracks


def has_track_info(text: str) -> bool:
    return bool(parse_tracks(text))
