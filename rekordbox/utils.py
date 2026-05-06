"""Small shared utilities for code that touches rekordbox's master.db.

Lives here (rather than in playlist/ or helpers/) because both the playlist
push code and the various helpers/* scripts need them.
"""
from __future__ import annotations

from typing import Optional

import psutil

# Beatport-streaming entries in rekordbox use FolderPath = "/v4/catalog/tracks/<bp_id>/".
BEATPORT_FOLDER_PREFIX = "/v4/catalog/tracks/"


def is_rekordbox_running() -> bool:
    """True if rekordbox.app is running. Master.db is locked while it's open;
    pyrekordbox writes will silently fail (or corrupt) without this guard."""
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == "rekordbox" or name.startswith("rekordbox "):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def beatport_id_from_folder_path(folder_path: Optional[str]) -> Optional[int]:
    """Extract the integer beatport_id from a Beatport-streaming `FolderPath`
    field on a `DjmdContent` row. Returns None for non-Beatport rows or
    unparseable paths."""
    if not folder_path or not folder_path.startswith(BEATPORT_FOLDER_PREFIX):
        return None
    try:
        return int(folder_path.split("/")[-2])
    except (ValueError, IndexError):
        return None
