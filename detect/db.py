"""SQLite persistence for track detection — all data in the unified dj.db."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / "Music" / "DJ.Studio" / "dj.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate() -> None:
    """Create all detect tables. Safe to run multiple times."""
    with _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS detected_tracks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                artist          TEXT,
                title           TEXT,
                shazam_key      TEXT,
                apple_music_id  TEXT,
                apple_music_url TEXT,
                source          TEXT,
                synced_at       TEXT NOT NULL,
                enrich_outcome  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_detected_shazam ON detected_tracks(shazam_key);

            CREATE TABLE IF NOT EXISTS sessions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                type                  TEXT    NOT NULL,
                url                   TEXT    NOT NULL,
                title                 TEXT,
                uploader              TEXT,
                caption               TEXT,
                duration_seconds      INTEGER,
                last_scanned_position INTEGER,
                started_at            TEXT    NOT NULL,
                ended_at              TEXT,
                UNIQUE(url)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(type);

            CREATE TABLE IF NOT EXISTS track_sessions (
                track_id   INTEGER NOT NULL REFERENCES detected_tracks(id) ON DELETE CASCADE,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                position   INTEGER,
                PRIMARY KEY (track_id, session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ts_session ON track_sessions(session_id);

            CREATE TABLE IF NOT EXISTS enriched_tracks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_track_id INTEGER UNIQUE REFERENCES detected_tracks(id) ON DELETE CASCADE,
                beatport_id       INTEGER NOT NULL,
                beatport_link     TEXT,
                bpm               REAL,
                key               TEXT,
                genre             TEXT,
                release_date      TEXT,
                apple_music_url   TEXT,
                artist            TEXT,
                title             TEXT,
                mik_key           TEXT,
                mik_nrg           REAL,
                vocals            TEXT,
                drums             TEXT,
                melody            TEXT,
                enriched_at       TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_enriched_detected ON enriched_tracks(detected_track_id);
            CREATE INDEX IF NOT EXISTS idx_enriched_beatport_id ON enriched_tracks(beatport_id);

            CREATE TABLE IF NOT EXISTS beatport_playlists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                beatport_id INTEGER NOT NULL UNIQUE,
                name        TEXT    NOT NULL,
                synced_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS beatport_playlist_tracks (
                playlist_id       INTEGER NOT NULL REFERENCES beatport_playlists(id) ON DELETE CASCADE,
                enriched_track_id INTEGER NOT NULL REFERENCES enriched_tracks(id) ON DELETE CASCADE,
                PRIMARY KEY (playlist_id, enriched_track_id)
            );

            CREATE INDEX IF NOT EXISTS idx_bpt_playlist ON beatport_playlist_tracks(playlist_id);
            CREATE INDEX IF NOT EXISTS idx_bpt_track ON beatport_playlist_tracks(enriched_track_id);

            CREATE TABLE IF NOT EXISTS enrich_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                seen        INTEGER DEFAULT 0,
                found       INTEGER DEFAULT 0,
                not_found   INTEGER DEFAULT 0,
                fuzzy_miss  INTEGER DEFAULT 0,
                status      TEXT
            );

            CREATE TABLE IF NOT EXISTS deleted_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   INTEGER NOT NULL,
                type         TEXT    NOT NULL,
                url          TEXT    NOT NULL,
                title        TEXT,
                uploader     TEXT,
                track_count  INTEGER NOT NULL DEFAULT 0,
                started_at   TEXT,
                deleted_at   TEXT    NOT NULL
            );
        """)
        try:
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_detected_shazam_key
                ON detected_tracks(shazam_key) WHERE shazam_key IS NOT NULL
            """)
        except Exception:
            pass

        # ── Additive migrations: rich-analysis columns on enriched_tracks ────
        # These columns are populated by `dj detect import-to-studio` (Path A
        # SDK pipeline). Adding them on existing DBs is a no-op when columns
        # already exist. Each is one-source per column so future pipelines
        # (e.g. rekordbox PSSI import) can populate their own without
        # conflict — extensibility by addition.
        _ENRICHED_RICH_COLS = [
            ("mik_key_secondary",   "TEXT"),
            ("mik_key_confidence",  "REAL"),
            ("tempo_precise",       "REAL"),
            ("duration_sec",        "REAL"),
            ("cue_points_count",    "INTEGER"),
            ("vocals_avg",          "REAL"),
            ("drums_avg",           "REAL"),
            ("bass_avg",            "REAL"),
            ("melody_avg",          "REAL"),
            ("vocals_peak",         "REAL"),
            ("drums_peak",          "REAL"),
            ("bass_peak",           "REAL"),
            ("melody_peak",         "REAL"),
            ("mix_name",            "TEXT"),
            ("label",               "TEXT"),
            ("catalog_number",      "TEXT"),
            ("isrc",                "TEXT"),
            ("sub_genre",           "TEXT"),
            ("length_ms",           "INTEGER"),
            ("analysis_json",       "TEXT"),
            # Rekordbox enrichment (added by import-rekordbox-analysis):
            ("rk_analysis_json",      "TEXT"),  # phrases (PSSI) + memory cues + hot cues + mood
            # Per-source completion timestamps. NULL = not done; ISO8601 = done.
            # Lets each pipeline own a single column for skip/re-run logic.
            ("dj_studio_at",          "TEXT"),  # import-to-studio finished (DJ Studio analysis)
            ("rekordbox_export_at",   "TEXT"),  # export-to-rekordbox pushed track + playlist entry
            ("rekordbox_analysis_at", "TEXT"),  # import-rekordbox-analysis ingested ANLZ data
        ]
        for col, typ in _ENRICHED_RICH_COLS:
            _add_column_if_missing(con, "enriched_tracks", col, typ)
            # Mirror to enriched_tracks_test if it exists. Created via
            # CREATE TABLE AS, so it inherits whatever columns enriched_tracks
            # had at seed time — but later migrations need to be re-applied.
            _add_column_if_missing(con, "enriched_tracks_test", col, typ)


def _add_column_if_missing(con: sqlite3.Connection, table: str, col: str, typ: str) -> None:
    """ALTER TABLE ADD COLUMN if not already present. Idempotent."""
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # table may not exist yet (e.g. enriched_tracks_test pre-creation)


# ── Unified session helpers ───────────────────────────────────────────────────


def create_session(
    type_: str,
    url: str,
    title: str,
    uploader: str | None = None,
    duration_seconds: int | None = None,
    caption: str | None = None,
) -> int:
    """Insert or return existing session for this URL. Returns session id."""
    with _connect() as con:
        con.execute(
            """INSERT OR IGNORE INTO sessions
               (type, url, title, uploader, duration_seconds, caption, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (type_, url, title, uploader, duration_seconds, caption, _now()),
        )
        row = con.execute("SELECT id FROM sessions WHERE url = ?", (url,)).fetchone()
        return row["id"]


def end_session(session_id: int) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), session_id)
        )


def update_session_progress(session_id: int, position: int) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE sessions SET last_scanned_position = ? WHERE id = ?",
            (position, session_id),
        )


def find_session(url: str) -> sqlite3.Row | None:
    with _connect() as con:
        return con.execute("SELECT * FROM sessions WHERE url = ?", (url,)).fetchone()


def infer_last_position(session_id: int) -> int | None:
    with _connect() as con:
        row = con.execute(
            "SELECT MAX(ts.position) AS p FROM track_sessions ts WHERE ts.session_id = ?",
            (session_id,),
        ).fetchone()
        return row["p"] if row and row["p"] is not None else None


def delete_session(session_id: int) -> int:
    """Delete session; tracks exclusively belonging to it (no other sessions, no enrichment) are also deleted."""
    with _connect() as con:
        session = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        n = con.execute(
            "SELECT COUNT(*) FROM track_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if session:
            con.execute(
                """INSERT INTO deleted_sessions
                   (session_id, type, url, title, uploader, track_count, started_at, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, session["type"], session["url"], session["title"],
                 session["uploader"], n, session["started_at"], _now()),
            )
        con.execute("""
            DELETE FROM detected_tracks
            WHERE id IN (SELECT track_id FROM track_sessions WHERE session_id = ?)
              AND id NOT IN (SELECT track_id FROM track_sessions WHERE session_id != ?)
              AND id NOT IN (SELECT detected_track_id FROM enriched_tracks
                             WHERE detected_track_id IS NOT NULL)
              AND source != 'beatport'
        """, (session_id, session_id))
        con.execute("DELETE FROM track_sessions WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return n


def list_sessions(type_: str, limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT s.*, COUNT(ts.track_id) AS track_count
               FROM sessions s
               LEFT JOIN track_sessions ts ON ts.session_id = s.id
               WHERE s.type = ?
               GROUP BY s.id
               ORDER BY s.started_at DESC
               LIMIT ?""",
            (type_, limit),
        ).fetchall()


# ── Track helpers ─────────────────────────────────────────────────────────────


def insert_track(
    track: dict,
    *,
    source: str,
    session_id: int | None = None,
) -> int:
    """Insert or find a detected track (globally deduped). Link to session if provided."""
    with _connect() as con:
        shazam_key = track.get("shazam_key")
        artist     = track.get("artist")
        title      = track.get("title")
        position   = track.get("position")

        existing = None
        if shazam_key:
            existing = con.execute(
                "SELECT id FROM detected_tracks WHERE shazam_key = ?", (shazam_key,)
            ).fetchone()
        if not existing and artist and title:
            existing = con.execute(
                "SELECT id FROM detected_tracks "
                "WHERE artist = ? AND title = ? AND shazam_key IS NULL",
                (artist, title),
            ).fetchone()

        if existing:
            track_id = existing["id"]
        else:
            cur = con.execute(
                """INSERT INTO detected_tracks
                   (artist, title, shazam_key, apple_music_id, apple_music_url, source, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (artist, title, shazam_key,
                 track.get("apple_music_id"), track.get("apple_music_url"),
                 source, _now()),
            )
            track_id = cur.lastrowid

        if session_id is not None:
            con.execute(
                "INSERT OR IGNORE INTO track_sessions (track_id, session_id, position) "
                "VALUES (?, ?, ?)",
                (track_id, session_id, position),
            )

        return track_id


def insert_tracks(
    tracks: list[dict],
    *,
    source: str,
    session_id: int | None = None,
) -> None:
    for t in tracks:
        insert_track(t, source=source, session_id=session_id)


def list_tracks(limit: int = 50) -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT * FROM detected_tracks
               WHERE enrich_outcome IS NULL OR enrich_outcome != 'duplicate'
               ORDER BY synced_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def tracks_for_session(session_id: int) -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT d.*, ts.position
               FROM detected_tracks d
               JOIN track_sessions ts ON ts.track_id = d.id
               WHERE ts.session_id = ?
               ORDER BY ts.position, d.id""",
            (session_id,),
        ).fetchall()


def tracks_for_session_enriched(session_id: int) -> list[sqlite3.Row]:
    """All tracks for a session with enrichment data where available.

    Duplicate-outcome tracks (same beatport_id found via a different detected_track)
    are resolved by falling back to an artist+title match in enriched_tracks so the
    full BPM/key/etc. are still returned.
    """
    with _connect() as con:
        return con.execute(
            """SELECT d.id, ts.position, d.enrich_outcome,
                      COALESCE(ed.artist, ei.artist, d.artist) AS artist,
                      COALESCE(ed.title,  ei.title,  d.title)  AS title,
                      COALESCE(ed.apple_music_url, d.apple_music_url) AS apple_music_url,
                      COALESCE(ed.beatport_id,   ei.beatport_id)   AS beatport_id,
                      COALESCE(ed.beatport_link, ei.beatport_link) AS beatport_link,
                      COALESCE(ed.bpm,           ei.bpm)           AS bpm,
                      COALESCE(ed.key,           ei.key)           AS key,
                      COALESCE(ed.genre,         ei.genre)         AS genre,
                      COALESCE(ed.release_date,  ei.release_date)  AS release_date,
                      COALESCE(ed.mik_key,       ei.mik_key)       AS mik_key,
                      COALESCE(ed.mik_nrg,       ei.mik_nrg)       AS mik_nrg,
                      COALESCE(ed.vocals,        ei.vocals)        AS vocals,
                      COALESCE(ed.drums,         ei.drums)         AS drums,
                      COALESCE(ed.melody,        ei.melody)        AS melody
               FROM detected_tracks d
               JOIN track_sessions ts ON ts.track_id = d.id
               LEFT JOIN enriched_tracks ed ON ed.detected_track_id = d.id
               LEFT JOIN enriched_tracks ei ON ei.id = (
                   SELECT e2.id FROM enriched_tracks e2
                   WHERE LOWER(e2.artist) = LOWER(d.artist)
                     AND LOWER(e2.title)  = LOWER(d.title)
                   LIMIT 1
               )
               WHERE ts.session_id = ?
               ORDER BY ts.position, d.id""",
            (session_id,),
        ).fetchall()


# ── Enrichment helpers ────────────────────────────────────────────────────────


def get_unenriched_tracks() -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT d.* FROM detected_tracks d
               LEFT JOIN enriched_tracks e ON e.detected_track_id = d.id
               WHERE e.id IS NULL
                 AND d.enrich_outcome IS NULL
                 AND d.artist IS NOT NULL AND d.title IS NOT NULL
               ORDER BY d.id""",
        ).fetchall()


def get_retry_tracks() -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT * FROM detected_tracks
               WHERE enrich_outcome IN ('not_found', 'fuzzy_miss')
                 AND artist IS NOT NULL AND title IS NOT NULL
               ORDER BY id""",
        ).fetchall()


def list_enriched_tracks(limit: int = 50, playlist_name: str | None = None) -> list[sqlite3.Row]:
    with _connect() as con:
        if playlist_name:
            return con.execute(
                """SELECT e.artist, e.title, e.beatport_id, e.beatport_link,
                          e.bpm, e.key, e.genre, e.release_date, e.apple_music_url,
                          e.mik_key, e.mik_nrg, e.vocals, e.drums, e.melody, e.enriched_at
                   FROM enriched_tracks e
                   JOIN beatport_playlist_tracks bpt ON bpt.enriched_track_id = e.id
                   JOIN beatport_playlists bp ON bp.id = bpt.playlist_id
                   WHERE bp.name = ?
                   ORDER BY e.enriched_at DESC, e.id DESC
                   LIMIT ?""",
                (playlist_name, limit),
            ).fetchall()
        return con.execute(
            """SELECT e.artist, e.title, e.beatport_id, e.beatport_link,
                      e.bpm, e.key, e.genre, e.release_date, e.apple_music_url,
                      e.mik_key, e.mik_nrg, e.vocals, e.drums, e.melody, e.enriched_at
               FROM enriched_tracks e
               ORDER BY e.enriched_at DESC, e.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def upsert_enriched(detected_track_id: int, meta: dict) -> None:
    with _connect() as con:
        # If this beatport_id is already enriched via a different detected_track,
        # mark this one as a duplicate and skip — keeps enriched_tracks deduplicated.
        beatport_id = meta.get("beatport_id")
        if beatport_id:
            existing = con.execute(
                "SELECT id FROM enriched_tracks WHERE beatport_id = ? AND detected_track_id != ?",
                (beatport_id, detected_track_id),
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE detected_tracks SET enrich_outcome = 'duplicate' WHERE id = ?",
                    (detected_track_id,),
                )
                return

        row = con.execute(
            "SELECT artist, title, apple_music_url FROM detected_tracks WHERE id = ?",
            (detected_track_id,),
        ).fetchone()
        artist = row["artist"] if row else None
        title = row["title"] if row else None
        apple_url = row["apple_music_url"] if row else None

        con.execute(
            """INSERT INTO enriched_tracks
               (detected_track_id, beatport_id, beatport_link, bpm, key, genre,
                release_date, apple_music_url, artist, title, enriched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(detected_track_id) DO UPDATE SET
                 beatport_id     = excluded.beatport_id,
                 beatport_link   = excluded.beatport_link,
                 bpm             = excluded.bpm,
                 key             = excluded.key,
                 genre           = excluded.genre,
                 release_date    = excluded.release_date,
                 apple_music_url = COALESCE(excluded.apple_music_url, enriched_tracks.apple_music_url),
                 artist          = COALESCE(excluded.artist, enriched_tracks.artist),
                 title           = COALESCE(excluded.title, enriched_tracks.title),
                 enriched_at     = excluded.enriched_at""",
            (
                detected_track_id,
                meta.get("beatport_id"),
                meta.get("beatport_link"),
                meta.get("bpm"),
                meta.get("key"),
                meta.get("genre"),
                meta.get("release_date"),
                apple_url,
                artist,
                title,
                _now(),
            ),
        )


def mark_enrich_miss(detected_track_id: int, outcome: str) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE detected_tracks SET enrich_outcome = ? WHERE id = ?",
            (outcome, detected_track_id),
        )


def start_enrich_run() -> int:
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO enrich_runs (started_at, status) VALUES (?, 'running')", (_now(),)
        )
        return cur.lastrowid


def finish_enrich_run(
    run_id: int, seen: int, found: int, not_found: int, fuzzy_miss: int
) -> None:
    with _connect() as con:
        con.execute(
            """UPDATE enrich_runs
               SET finished_at=?, seen=?, found=?, not_found=?, fuzzy_miss=?, status='done'
               WHERE id=?""",
            (_now(), seen, found, not_found, fuzzy_miss, run_id),
        )


def list_enrich_runs(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            """SELECT * FROM enrich_runs ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_synced_beatport_ids() -> set[int]:
    with _connect() as con:
        rows = con.execute(
            "SELECT beatport_id FROM enriched_tracks WHERE beatport_id IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}


def upsert_beatport_playlist(beatport_id: int, name: str) -> int:
    """Upsert a Beatport playlist record. Returns the local row id."""
    with _connect() as con:
        con.execute(
            """INSERT INTO beatport_playlists (beatport_id, name, synced_at)
               VALUES (?, ?, ?)
               ON CONFLICT(beatport_id) DO UPDATE SET name=excluded.name, synced_at=excluded.synced_at""",
            (beatport_id, name, _now()),
        )
        row = con.execute(
            "SELECT id FROM beatport_playlists WHERE beatport_id = ?", (beatport_id,)
        ).fetchone()
        return row["id"]


def insert_beatport_track(
    artist: str,
    title: str,
    beatport_link: str,
    meta: dict,
    playlist_id: int | None = None,
) -> bool:
    """Upsert a track from a Beatport playlist into enriched_tracks.

    Writes artist/title directly — no detected_tracks row is created.
    If playlist_id is provided, records the playlist link in beatport_playlist_tracks.

    Returns True if a new enriched_tracks row was created OR a new playlist link was added.
    """
    beatport_id = meta.get("beatport_id")
    with _connect() as con:
        row = con.execute(
            "SELECT id FROM enriched_tracks WHERE beatport_id = ?", (beatport_id,)
        ).fetchone()

        if row:
            enriched_id = row["id"]
            newly_inserted = False
        else:
            cur = con.execute(
                """INSERT INTO enriched_tracks
                   (beatport_id, beatport_link, bpm, key, genre,
                    release_date, artist, title, enriched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    beatport_id, beatport_link,
                    meta.get("bpm"), meta.get("key"), meta.get("genre"),
                    meta.get("release_date"), artist, title, _now(),
                ),
            )
            enriched_id = cur.lastrowid
            newly_inserted = True

        newly_linked = False
        if playlist_id is not None:
            existing_link = con.execute(
                "SELECT 1 FROM beatport_playlist_tracks WHERE playlist_id = ? AND enriched_track_id = ?",
                (playlist_id, enriched_id),
            ).fetchone()
            if not existing_link:
                con.execute(
                    "INSERT OR IGNORE INTO beatport_playlist_tracks (playlist_id, enriched_track_id) VALUES (?, ?)",
                    (playlist_id, enriched_id),
                )
                newly_linked = True

        return newly_inserted or newly_linked


_STUDIO_TABLES = frozenset({"enriched_tracks", "enriched_tracks_test"})


def get_studio_enrichable_tracks(table: str = "enriched_tracks") -> list[sqlite3.Row]:
    """Tracks for `dj detect enrich-studio` to read DJ Studio's library.

    Skip rule: tracks that already have mik_key set (existing semantics).
    For the heavier `dj detect import-to-studio` pipeline, see
    `get_import_to_studio_pending` which uses a stronger skip rule.
    """
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        return con.execute(
            f"""SELECT e.id, e.beatport_id, e.artist, e.title, e.bpm
                FROM {table} e
                WHERE e.mik_key IS NULL
                ORDER BY e.id""",
        ).fetchall()


def get_import_to_studio_pending(table: str = "enriched_tracks", *, force: bool = False) -> list[sqlite3.Row]:
    """Tracks pending Path A analysis (dj detect import-to-studio).

    Skip rule: `dj_studio_at IS NULL`. Pipeline sets that timestamp on
    successful run, so re-running the command picks up only new + previously-
    failed rows. Pass force=True to re-process every track.
    """
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        # Defensive: ensure dj_studio_at exists on this table (the migration
        # may not have run for enriched_tracks_test which is recreated fresh).
        _add_column_if_missing(con, table, "dj_studio_at", "TEXT")
        where = "" if force else "WHERE e.dj_studio_at IS NULL"
        return con.execute(
            f"""SELECT e.id, e.beatport_id, e.artist, e.title, e.bpm
                FROM {table} e
                {where}
                ORDER BY e.id""",
        ).fetchall()


def mark_pipeline_done(table: str, beatport_id: int, column: str) -> None:
    """Stamp a per-source completion column with the current ISO timestamp."""
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    if column not in {"dj_studio_at", "rekordbox_export_at", "rekordbox_analysis_at"}:
        raise ValueError(f"Unsupported column: {column}")
    with _connect() as con:
        _add_column_if_missing(con, table, column, "TEXT")
        con.execute(
            f"UPDATE {table} SET {column} = ? WHERE beatport_id = ?",
            (_now(), beatport_id),
        )


def get_export_to_rekordbox_pending(table: str = "enriched_tracks", *, force: bool = False) -> list[sqlite3.Row]:
    """Tracks not yet pushed into a rekordbox playlist.

    Skip rule: rekordbox_export_at IS NULL.
    """
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        _add_column_if_missing(con, table, "rekordbox_export_at", "TEXT")
        where = "" if force else "WHERE e.rekordbox_export_at IS NULL"
        return con.execute(
            f"""SELECT e.id, e.beatport_id, e.artist, e.title, e.bpm,
                       e.beatport_link, e.key, e.genre, e.duration_sec, e.mik_key
                  FROM {table} e
                  {where}
                  ORDER BY e.id""",
        ).fetchall()


def get_rekordbox_analysis_pending(table: str = "enriched_tracks", *, force: bool = False) -> list[sqlite3.Row]:
    """Tracks pushed to rekordbox but not yet ingested back from ANLZ.

    Skip rule: rekordbox_export_at IS NOT NULL  AND  rekordbox_analysis_at IS NULL.
    Won't try to ingest tracks that haven't been pushed yet.
    """
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        _add_column_if_missing(con, table, "rekordbox_export_at", "TEXT")
        _add_column_if_missing(con, table, "rekordbox_analysis_at", "TEXT")
        where = (
            "WHERE e.rekordbox_export_at IS NOT NULL"
            if force
            else "WHERE e.rekordbox_export_at IS NOT NULL AND e.rekordbox_analysis_at IS NULL"
        )
        return con.execute(
            f"""SELECT e.id, e.beatport_id, e.artist, e.title
                  FROM {table} e
                  {where}
                  ORDER BY e.id""",
        ).fetchall()


def update_rk_analysis_json(table: str, beatport_id: int, blob: str) -> None:
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        _add_column_if_missing(con, table, "rk_analysis_json", "TEXT")
        con.execute(
            f"UPDATE {table} SET rk_analysis_json = ? WHERE beatport_id = ?",
            (blob, beatport_id),
        )


def update_studio_enrich(enriched_id: int, data: dict, table: str = "enriched_tracks") -> None:
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    with _connect() as con:
        con.execute(
            f"""UPDATE {table}
               SET mik_key=?, mik_nrg=?, vocals=?, drums=?, melody=?
               WHERE id=?""",
            (data.get("mik_key"), data.get("mik_nrg"), data.get("vocals"),
             data.get("drums"), data.get("melody"), enriched_id),
        )


def create_enriched_tracks_test(limit: int = 100) -> int:
    """Drop and recreate enriched_tracks_test with `limit` most-recently-enriched rows.

    Schema mirrors enriched_tracks 1:1 (every column copies through), so:
      - rich-analysis fields already populated on production rows carry over
        and are NOT re-processed when `import-to-studio` runs against the test
        table (idempotence in re-runs).
      - completion timestamps (dj_studio_at / rekordbox_*_at) carry over too,
        so a track already analyzed in production stays "done" in the sandbox.
    """
    with _connect() as con:
        con.execute("DROP TABLE IF EXISTS enriched_tracks_test")
        # Mirror production: SQLite's CREATE TABLE AS preserves columns + types.
        # We add the AUTOINCREMENT id afterwards by recreating with ROWID alias.
        con.execute("""
            CREATE TABLE enriched_tracks_test AS
            SELECT * FROM enriched_tracks WHERE 1 = 0
        """)
        con.execute("CREATE INDEX idx_ett_beatport_id ON enriched_tracks_test(beatport_id)")
        # Copy over the most-recently-enriched rows AS-IS (every column).
        con.execute(
            """
            INSERT INTO enriched_tracks_test
            SELECT * FROM enriched_tracks
            WHERE beatport_id IS NOT NULL
            ORDER BY enriched_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return con.execute("SELECT COUNT(*) FROM enriched_tracks_test").fetchone()[0]


def update_enriched_rich(beatport_id: int, fields: dict, table: str = "enriched_tracks") -> None:
    """Update the rich-analysis columns for one row in enriched_tracks (or _test).

    Only writes columns that exist on the table; silently ignores unknown keys.
    """
    if table not in _STUDIO_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    allowed = {
        "mik_key_secondary", "mik_key_confidence", "tempo_precise", "duration_sec",
        "cue_points_count",
        "vocals_avg", "drums_avg", "bass_avg", "melody_avg",
        "vocals_peak", "drums_peak", "bass_peak", "melody_peak",
        "mix_name", "label", "catalog_number", "isrc", "sub_genre", "length_ms",
        "analysis_json",
    }
    cols = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not cols:
        return
    setters = ", ".join(f"{k} = ?" for k in cols)
    with _connect() as con:
        con.execute(
            f"UPDATE {table} SET {setters} WHERE beatport_id = ?",
            (*cols.values(), beatport_id),
        )


# Backward-compat alias (older callers / tests still reference this name).
update_enriched_tracks_test_rich = lambda bid, fields: update_enriched_rich(bid, fields, "enriched_tracks_test")
