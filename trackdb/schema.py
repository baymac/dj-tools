"""Track metadata SQLite schema, paths, and validators."""

import sqlite3
from pathlib import Path


DB_PATH = Path.home() / "Music" / "DJ.Studio" / "track_metadata.db"

SECTION_TYPES = {
    "intro", "buildup", "drop", "breakdown", "outro", "bridge", "verse", "chorus",
}
INTENSITY_LEVELS = {"none", "low", "medium", "high"}

# DJ Studio numeric key (1-24) → Camelot
CAMELOT_MAP = {
    1: "8B",  2: "3B",  3: "10B", 4: "5B",  5: "12B", 6: "7B",
    7: "2B",  8: "9B",  9: "4B",  10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A",
    19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A", 24: "10A",
}


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            id           INTEGER PRIMARY KEY,
            library_key  TEXT UNIQUE NOT NULL,
            beatport_id  TEXT,
            beatport_url TEXT,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL,
            genre        TEXT,
            key          TEXT,
            bpm          REAL,
            energy       INTEGER CHECK(energy BETWEEN 1 AND 10),
            vocals       TEXT CHECK(vocals IN ('none', 'low', 'medium', 'high')),
            drums        TEXT CHECK(drums  IN ('none', 'low', 'medium', 'high')),
            melody       TEXT CHECK(melody IN ('none', 'low', 'medium', 'high')),
            notes        TEXT,
            release_date TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_sections (
            id           INTEGER PRIMARY KEY,
            track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            section_type TEXT NOT NULL CHECK(section_type IN (
                'intro', 'buildup', 'drop', 'breakdown', 'outro',
                'bridge', 'verse', 'chorus'
            )),
            start_beat   REAL NOT NULL,
            end_beat     REAL,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_beatport  ON tracks(beatport_id);
        CREATE INDEX IF NOT EXISTS idx_sections_track   ON track_sections(track_id);
        CREATE INDEX IF NOT EXISTS idx_sections_type    ON track_sections(section_type);
    """)
    for col, defn in [
        ("beatport_url", "TEXT"),
        ("release_date", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
