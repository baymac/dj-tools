"""Generic text tracklist extractor — paste into vi, we parse on exit.

No URL needed: takes a free-form session name. Handles messy copy-paste input:
timestamps at line starts, ALL-CAPS boundary splits for merged lines, etc.
Skips lines it can't confidently parse and reports them so the user can fix manually.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import DB_PATH

# artist - title separator
_SEP_RE = re.compile(r"\s+[-–—]\s+")
# labels in [brackets] at end
_LABEL_RE = re.compile(r"\s*\[[^\]]{1,50}\]\s*$")
# leading timestamp: [00:00] / 00:00 / (1:23:45) etc.
_TS_RE = re.compile(r"^[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*[-–—]?\s*")
# leading position number: "1." / "2)" / "01."
_POS_RE = re.compile(r"^\d+[.)]\s*")
# "w/" overlay prefix: "w/ Artist – Title"
_W_PREFIX_RE = re.compile(r"^w/\s*", re.IGNORECASE)
# skip "ID – ID" placeholder lines
_ID_LINE_RE = re.compile(r"^ID\s*[-–—]\s*ID$", re.IGNORECASE)
# bare URLs
_URL_RE = re.compile(r"^https?://\S+$")
# comment / markdown markers
_SKIP_PREFIX_RE = re.compile(r"^[>#*_~`|]")
# ALL-CAPS word boundary (detects merged lines like "BOOTYbritney → BOOTY + britney")
_CAPS_BOUNDARY_RE = re.compile(r"([A-Z]{2,})([a-z])")


def _split_merged(line: str) -> list[str]:
    """Split lines where two tracks were pasted together at an ALL-CAPS boundary.

    Example: "donnie x slowrolla - BOOTYbritney spears - gimme more"
    → ["donnie x slowrolla - BOOTY", "britney spears - gimme more"]
    """
    m = _CAPS_BOUNDARY_RE.search(line)
    if m:
        left = line[: m.start() + len(m.group(1))].strip()
        right = line[m.start() + len(m.group(1)) :].strip()
        if _SEP_RE.search(left) and _SEP_RE.search(right):
            return [left, right]
    return [line]


def _clean_line(line: str) -> str:
    line = _TS_RE.sub("", line.strip())
    line = _POS_RE.sub("", line.strip())
    line = _W_PREFIX_RE.sub("", line.strip())
    line = _LABEL_RE.sub("", line)
    return line.strip()


def _parse_line(line: str) -> Optional[dict]:
    """Return {artist, title} for a track line, or None if not parseable."""
    if not line or _SKIP_PREFIX_RE.match(line) or _URL_RE.match(line):
        return None
    if len(line) > 300:
        return None
    cleaned = _clean_line(line)
    if not cleaned or _ID_LINE_RE.match(cleaned):
        return None
    m = _SEP_RE.search(cleaned)
    if not m:
        return None
    artist = cleaned[: m.start()].strip()
    title = cleaned[m.end() :].strip()
    if not artist or not title:
        return None
    if len(artist) > 150 or len(title) > 200:
        return None
    if len(artist.split()) > 10:
        return None
    return {"artist": artist, "title": title}


def extract_from_text(text: str) -> tuple[list[dict], list[str]]:
    """Parse all track lines. Returns (tracks, skipped_lines)."""
    tracks: list[dict] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    pos = 0

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue

        candidates = _split_merged(raw)
        parsed_any = False
        for candidate in candidates:
            track = _parse_line(candidate)
            if track:
                key = (track["artist"].lower(), track["title"].lower())
                if key not in seen:
                    seen.add(key)
                    pos += 1
                    track["position"] = pos
                    tracks.append(track)
                parsed_any = True

        if not parsed_any:
            skipped.append(raw)

    return tracks, skipped


def open_editor_for_session(name: str) -> str:
    """Open vi with a header pre-filled; return file contents after exit."""
    db_dir = DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9_-]", "_", name.lower())[:60]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    paste_file = db_dir / f"text_{slug}_{ts}.txt"

    header = (
        f"# Paste the tracklist below, then save and quit (:wq)\n"
        f"# Session: {name}\n"
        f"# Lines like  'Artist - Title (Mix) [Label]'  will be extracted.\n"
        f"# Timestamps at line start (00:00, [1:23:45]) are stripped automatically.\n"
        f"# Lines without a  ' - '  separator are skipped and reported.\n"
        f"#\n"
    )
    paste_file.write_text(header)
    subprocess.run(["vi", str(paste_file)])
    return paste_file.read_text()
