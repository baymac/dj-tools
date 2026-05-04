"""Backup rekordbox's master.db before destructive writes."""

import datetime
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .constants import RB_BACKUP_DIR, RB_DB_PATH


def rekordbox_running() -> bool:
    return subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True).returncode == 0


def backup_db(label: str) -> Optional[Path]:
    """Copy master.db to a timestamped backup; returns the destination path."""
    if not RB_DB_PATH.exists():
        return None
    RB_BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^\w-]", "_", label)[:40].strip("_")
    dest = RB_BACKUP_DIR / f"{ts}_{slug}.db"
    shutil.copy2(RB_DB_PATH, dest)
    return dest
