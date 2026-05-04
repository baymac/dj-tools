"""Reddit post track list extractor — paste the post text into vi, we parse on exit."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .db import DB_PATH

# Separators between artist and title
_SEP_RE = re.compile(r"\s+[-–—]\s+")
# Labels in square brackets at end: "[Drumcode]" / "[ Kompakt ]"
_LABEL_RE = re.compile(r"\s*\[[^\]]{1,50}\]\s*$")
# Reddit markdown link: [text](url) → keep text
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")
# Bare URL on its own
_URL_RE = re.compile(r"^https?://\S+$")
# Leading position number: "1. " / "2) "
_POS_RE = re.compile(r"^\d+[.)]\s*")
# Reddit quote / header markers
_SKIP_PREFIX_RE = re.compile(r"^[>#*_~`|]")


def _clean_line(line: str) -> str:
    line = _MD_LINK_RE.sub(r"\1", line)
    line = _POS_RE.sub("", line.strip())
    line = _LABEL_RE.sub("", line)
    return line.strip()


def _parse_line(line: str) -> Optional[dict]:
    """Return {artist, title} for a track line, or None if not a track."""
    if not line or _SKIP_PREFIX_RE.match(line) or _URL_RE.match(line):
        return None
    if len(line) > 250:
        return None
    cleaned = _clean_line(line)
    if not cleaned:
        return None
    m = _SEP_RE.search(cleaned)
    if not m:
        return None
    artist = cleaned[: m.start()].strip()
    title = cleaned[m.end() :].strip()
    if not artist or not title:
        return None
    if len(artist) > 120 or len(title) > 180:
        return None
    if len(artist.split()) > 8:
        return None
    return {"artist": artist, "title": title}


def extract_from_text(text: str) -> list[dict]:
    """Parse all track lines from a block of text; deduplicate by (artist, title)."""
    tracks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    pos = 0
    for line in text.splitlines():
        track = _parse_line(line.strip())
        if track:
            key = (track["artist"].lower(), track["title"].lower())
            if key not in seen:
                seen.add(key)
                pos += 1
                track["position"] = pos
                tracks.append(track)
    return tracks


def open_editor_for_post(url: str) -> str:
    """
    Create a file next to dj.db, open it in vi for the user to paste the post
    body, and return the file contents after vi exits.
    """
    db_dir = DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    # Derive a filename from the URL slug
    slug = url.rstrip("/").split("/")[-1] or "reddit_post"
    slug = re.sub(r"[^a-z0-9_-]", "_", slug.lower())[:60]
    paste_file = db_dir / f"reddit_{slug}.txt"

    header = (
        f"# Paste the Reddit post body below, then save and quit (:wq)\n"
        f"# URL: {url}\n"
        f"# Lines like  '1. Artist - Title (Mix) [Label]'  will be extracted.\n"
        f"# Labels in [brackets] and position numbers are stripped automatically.\n"
        f"#\n"
    )
    paste_file.write_text(header)

    subprocess.run(["vi", str(paste_file)])

    return paste_file.read_text()
