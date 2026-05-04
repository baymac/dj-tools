"""SQLite persistence for the Apple Music → Beatport sync CLI (unified dj.db)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / "Music" / "DJ.Studio" / "dj.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_tracks (
    catalog_id        TEXT    NOT NULL PRIMARY KEY,
    beatport_track_id INTEGER,
    source_playlist   TEXT    NOT NULL,
    dest_playlist     TEXT,
    outcome           TEXT    NOT NULL,
    synced_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_playlist TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    tracks_seen     INTEGER DEFAULT 0,
    tracks_added    INTEGER DEFAULT 0,
    tracks_skipped  INTEGER DEFAULT 0,
    tracks_failed   INTEGER DEFAULT 0,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS auth_cache (
    service      TEXT NOT NULL PRIMARY KEY,
    token        TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    expires_at   TEXT
);

CREATE TABLE IF NOT EXISTS cursors (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_TERMINAL_OUTCOMES = (
    "added", "duplicate", "no_classify", "no_search_results",
    "no_catalog_match", "fuzzy_miss", "noop_empty_items",
)


@contextmanager
def _conn(db_path: Path = DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with _conn(db_path) as con:
        con.executescript(_SCHEMA)


def load_synced_set(source_playlist: str, db_path: Path = DB_PATH) -> set[str]:
    """Return catalog_ids that should be skipped for source_playlist."""
    placeholders = ",".join("?" * len(_TERMINAL_OUTCOMES))
    with _conn(db_path) as con:
        rows = con.execute(
            f"SELECT catalog_id FROM synced_tracks WHERE source_playlist = ? AND outcome IN ({placeholders})",
            (source_playlist, *_TERMINAL_OUTCOMES),
        ).fetchall()
    return {r["catalog_id"] for r in rows}


def mark_synced(
    catalog_id: str,
    source_playlist: str,
    outcome: str,
    beatport_track_id: Optional[int] = None,
    dest_playlist: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as con:
        con.execute(
            """INSERT OR REPLACE INTO synced_tracks
               (catalog_id, beatport_track_id, source_playlist, dest_playlist, outcome, synced_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (catalog_id, beatport_track_id, source_playlist, dest_playlist, outcome, now),
        )


def start_sync_run(source_playlist: str, db_path: Path = DB_PATH) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as con:
        cur = con.execute(
            "INSERT INTO sync_runs (source_playlist, started_at, status) VALUES (?, ?, 'running')",
            (source_playlist, now),
        )
        return cur.lastrowid


def finish_sync_run(
    run_id: int,
    tracks_seen: int,
    tracks_added: int,
    tracks_skipped: int,
    tracks_failed: int,
    status: str = "done",
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as con:
        con.execute(
            """UPDATE sync_runs
               SET finished_at=?, tracks_seen=?, tracks_added=?,
                   tracks_skipped=?, tracks_failed=?, status=?
               WHERE id=?""",
            (now, tracks_seen, tracks_added, tracks_skipped, tracks_failed, status, run_id),
        )



def get_cursor(key: str, db_path: Path = DB_PATH) -> Optional[str]:
    """Return the stored cursor value for key, or None if not set."""
    with _conn(db_path) as con:
        row = con.execute("SELECT value FROM cursors WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_cursor(key: str, value: str, db_path: Path = DB_PATH) -> None:
    with _conn(db_path) as con:
        con.execute(
            "INSERT OR REPLACE INTO cursors (key, value) VALUES (?, ?)", (key, value)
        )


