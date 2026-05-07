"""Single source of truth for every file the dj tool reads or writes.

Layout under `~/Music/dj-tools/`:

    dj.db                              SQLite — all tables
    logs/<command>/YYYY-MM-DD_<id>.log per-command log files
    state/                             session files, config, browser profile
    cache/musickit/                    Swift bridge build cache
    exports/                           default for export-* helpers
    backups/apple-music/               default for backup_apple_music helper
    backups/rekordbox/                 master.db pre-write backups

Each writer is responsible for its own `mkdir(parents=True, exist_ok=True)` —
this module just exposes path constants and small log helpers.
"""
from __future__ import annotations

import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Tuple

DJ_TOOLS_DIR = Path.home() / "Music" / "dj-tools"

DB_PATH = DJ_TOOLS_DIR / "dj.db"
LOGS_DIR = DJ_TOOLS_DIR / "logs"
STATE_DIR = DJ_TOOLS_DIR / "state"
CACHE_DIR = DJ_TOOLS_DIR / "cache"
EXPORTS_DIR = DJ_TOOLS_DIR / "exports"
BACKUPS_DIR = DJ_TOOLS_DIR / "backups"

# State files
IG_SESSION_FILE = STATE_DIR / "ig_session.json"
DETECT_CONFIG_FILE = STATE_DIR / "detect_config.json"
BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"

# Cache
MUSICKIT_CACHE_DIR = CACHE_DIR / "musickit"

COURSE_DIR = DJ_TOOLS_DIR / "course"

# Exports & backups
APPLE_MUSIC_EXPORT_CSV = EXPORTS_DIR / "apple_music_export.csv"
APPLE_MUSIC_BACKUP_DIR = BACKUPS_DIR / "apple-music"
REKORDBOX_BACKUP_DIR = BACKUPS_DIR / "rekordbox"


def log_path(command: str, run_id: str | int) -> Path:
    """Return the log file path for one run of `command`. Creates the dir."""
    cmd_dir = LOGS_DIR / command
    cmd_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    return cmd_dir / f"{date_str}_{run_id}.log"


def open_log(command: str, run_id: str | int | None = None) -> Tuple[Path, IO[str]]:
    """Open a fresh log file for a command run. Returns (path, file_handle)."""
    if run_id is None:
        run_id = datetime.datetime.now().strftime("%H%M%S")
    p = log_path(command, run_id)
    return p, p.open("w", encoding="utf-8")


@contextmanager
def command_logger(
    command: str, console, run_id: str | int | None = None,
) -> Iterator[Path]:
    """Capture every rich.Console call inside this block to a log file.

    Sets `console.record = True` for the duration; at exit, dumps `export_text`
    to logs/<command>/<date>_<run_id>.log. Use as:

        with command_logger("studio-analyse", console) as log_path:
            console.print(f"[dim]Log: {log_path}[/dim]")
            ...  # whatever the command does

    Survives exceptions — log is flushed in finally. Safe if console is None
    (no-op).
    """
    if run_id is None:
        run_id = datetime.datetime.now().strftime("%H%M%S")
    p = log_path(command, run_id)

    if console is None:
        yield p
        return

    prev_record = getattr(console, "record", False)
    console.record = True
    try:
        yield p
    finally:
        try:
            text = console.export_text(clear=True)
            p.write_text(text, encoding="utf-8")
        except Exception:
            pass
        console.record = prev_record
