"""1001tracklists.com tracklist extractor — paste the page text into vi, we parse on exit.

Format:
  Header:  DJ @ Location (Event) YYYY-MM-DD
  Tracks:  [MM:SS] Artist - Title [LABEL]
           [H:MM:SS] Artist - Title [LABEL]
  Overlays: w/ Artist - Title [LABEL]   (inherit parent timestamp)

Reuses detect/text.py's parser which already handles timestamps, w/ lines, and [LABEL] stripping.
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

from .db import DB_PATH
from .text import extract_from_text  # noqa: F401  re-exported for callers

_HEADER_RE = re.compile(r"^([^#\[].+?)\s+\d{4}-\d{2}-\d{2}\s*$")


def extract_title_from_text(text: str) -> Optional[str]:
    """Return session title from the 'DJ @ Location YYYY-MM-DD' header line, or None."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _HEADER_RE.match(line)
        if m:
            return m.group(1).strip()
        # Stop at first track line — header must come before timestamps
        if re.match(r"^\[\d", line):
            break
    return None


def open_editor_for_url(url: str) -> str:
    """Create a paste file next to dj.db, open it in vi, return contents on exit."""
    db_dir = DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    slug = url.rstrip("/").split("/")[-1] or "1001tracklists"
    slug = re.sub(r"[^a-z0-9_-]", "_", slug.lower())[:60]
    paste_file = db_dir / f"1001tracklists_{slug}.txt"

    header = (
        f"# Paste the 1001tracklists.com tracklist below, then save and quit (:wq)\n"
        f"# URL: {url}\n"
        f"# First line: 'DJ @ Location (Event) YYYY-MM-DD'  — auto-detected as session title\n"
        f"# Track lines:   '[MM:SS] Artist - Title [Label]'\n"
        f"# Overlay lines: 'w/ Artist - Title [Label]'  (inherit parent timestamp)\n"
        f"# Lines without a ' - ' separator are skipped.\n"
        f"#\n"
    )
    paste_file.write_text(header)
    subprocess.run(["vi", str(paste_file)])
    return paste_file.read_text()
