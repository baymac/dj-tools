"""topdjmixes.com tracklist extractor — paste the page text into vi, we parse on exit.

Same parser shape as `detect/reddit.py`. The leading-position regex already
matches `01.`/`02.` zero-padded numbers and the separator regex matches the
em-dash that topdjmixes uses (`Artist – Title`).
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

from .db import DB_PATH

_SEP_RE = re.compile(r"\s+[-–—]\s+")
_LABEL_RE = re.compile(r"\s*\[[^\]]{1,50}\]\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")
_URL_RE = re.compile(r"^https?://\S+$")
_POS_RE = re.compile(r"^\d+[.)]\s*")
_SKIP_PREFIX_RE = re.compile(r"^[>#*_~`|]")


def _clean_line(line: str) -> str:
    line = _MD_LINK_RE.sub(r"\1", line)
    line = _POS_RE.sub("", line.strip())
    line = _LABEL_RE.sub("", line)
    return line.strip()


def _parse_line(line: str) -> Optional[dict]:
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
    """Create a paste file next to dj.db, open it in vi, return the contents on exit."""
    db_dir = DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    slug = url.rstrip("/").split("/")[-1] or "topdjmixes_post"
    slug = re.sub(r"[^a-z0-9_-]", "_", slug.lower())[:60]
    paste_file = db_dir / f"topdjmixes_{slug}.txt"

    header = (
        f"# Paste the topdjmixes.com tracklist below, then save and quit (:wq)\n"
        f"# URL: {url}\n"
        f"# Lines like  '01. Artist – Title (Mix) [Label]'  will be extracted.\n"
        f"# Position numbers and trailing [bracket] labels are stripped automatically.\n"
        f"#\n"
    )
    paste_file.write_text(header)

    subprocess.run(["vi", str(paste_file)])

    return paste_file.read_text()
