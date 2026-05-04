"""Run user SQL against the enriched tables, return full row dicts.

Push code (`playlist.to_rekordbox`, `playlist.to_djstudio`) needs columns the
user's query may not have selected (artist/title/genre/key/bpm/length_ms). After
extracting beatport_ids from the user's query, we re-fetch full rows from
`enriched_tracks` LEFT JOIN `enriched_tracks_analysis` so destinations always
have the fields they need regardless of how the user wrote their SQL.
"""
from __future__ import annotations

import sqlite3
from typing import Sequence

from detect import db as ddb


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(ddb.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def run_user_query(sql: str) -> list[int]:
    """Execute the user's SQL and return the beatport_ids it produces.

    The query MUST be a SELECT that produces a `beatport_id` column. The query
    runs against the user's dj.db with that connection's full privileges — this
    tool assumes the user owns the database and is the only caller. If a
    `beatport_id` column is not in the result set, we raise after fetch.
    """
    sql_stripped = sql.strip()
    sql_lower = sql_stripped.lower()
    if not (sql_lower.startswith("select ") or sql_lower.startswith("with ")):
        raise ValueError("Query must start with SELECT or WITH")

    with _connect() as con:
        rows = con.execute(sql_stripped).fetchall()

    seen: set[int] = set()
    out: list[int] = []
    for r in rows:
        try:
            bp = r["beatport_id"]
        except (IndexError, KeyError):
            raise ValueError("Query result has no 'beatport_id' column")
        if bp is None:
            continue
        bp_int = int(bp)
        if bp_int in seen:
            continue
        seen.add(bp_int)
        out.append(bp_int)
    return out


def fetch_full_rows(beatport_ids: Sequence[int]) -> list[dict]:
    """Re-fetch full rows for these beatport_ids, in input order.

    Joins `enriched_tracks` (Beatport-derived data) LEFT JOIN
    `enriched_tracks_analysis` (DJ Studio + rekordbox derived data) so callers
    get every column we have on the track. Tracks not in `enriched_tracks` are
    silently dropped from the result.
    """
    if not beatport_ids:
        return []
    placeholders = ",".join("?" * len(beatport_ids))
    with _connect() as con:
        rows = con.execute(
            f"""SELECT e.*, a.mik_key AS mik_key_analysis, a.mik_nrg, a.vocals, a.drums,
                       a.melody, a.tempo_precise, a.duration_sec, a.cue_points_count,
                       a.analysis_json, a.rk_analysis_json,
                       a.dj_studio_at, a.rekordbox_export_at, a.rekordbox_analysis_at
                FROM enriched_tracks e
                LEFT JOIN enriched_tracks_analysis a ON a.beatport_id = e.beatport_id
                WHERE e.beatport_id IN ({placeholders})""",
            list(beatport_ids),
        ).fetchall()
    by_bid: dict[int, dict] = {int(r["beatport_id"]): dict(r) for r in rows}
    return [by_bid[bp] for bp in beatport_ids if bp in by_bid]
