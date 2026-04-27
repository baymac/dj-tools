"""Backup and restore rekordbox's master.db before destructive writes."""

import datetime
import glob as _glob
import re
import shutil
import subprocess
import sys
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


def undo_list() -> None:
    if not RB_BACKUP_DIR.exists() or not list(RB_BACKUP_DIR.glob("*.db")):
        print(f"No backups found in {RB_BACKUP_DIR}")
        return
    backups = sorted(RB_BACKUP_DIR.glob("*.db"))
    print(f"Available backups ({RB_BACKUP_DIR}):\n")
    for b in backups:
        size_mb = b.stat().st_size / (1024 * 1024)
        print(f"  {b.name}  ({size_mb:.1f} MB)")
    print(f"\nRestore: uv run dj_cli.py undo restore BACKUP_NAME")


def undo_restore(backup_name: str) -> None:
    if rekordbox_running():
        print("ERROR: rekordbox is running. Close it before restoring.", file=sys.stderr)
        sys.exit(1)

    backup_path = RB_BACKUP_DIR / backup_name
    if not backup_path.exists():
        matches = list(RB_BACKUP_DIR.glob(f"*{_glob.escape(backup_name)}*"))
        if len(matches) == 1:
            backup_path = matches[0]
        elif len(matches) > 1:
            print(f"Ambiguous: multiple backups match '{backup_name}':", file=sys.stderr)
            for m in matches:
                print(f"  {m.name}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Backup not found: {backup_name}", file=sys.stderr)
            sys.exit(1)

    if not RB_DB_PATH.exists():
        print(f"rekordbox DB not found at {RB_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    pre = backup_db("pre-restore")
    if pre:
        print(f"Saved current DB as: {pre.name}")
    shutil.copy2(backup_path, RB_DB_PATH)
    print(f"Restored {backup_path.name}  →  {RB_DB_PATH}")
