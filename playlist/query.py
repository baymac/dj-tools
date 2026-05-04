"""Run user SQL against the enriched tables, return enriched_tracks_full rows."""
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
    if not sql_stripped.lower().startswith("select "):
        raise ValueError("Query must start with SELECT")

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
    """Re-fetch full rows from enriched_tracks_full, in the order of beatport_ids."""
    if not beatport_ids:
        return []
    placeholders = ",".join("?" * len(beatport_ids))
    with _connect() as con:
        rows = con.execute(
            f"SELECT * FROM enriched_tracks_full WHERE beatport_id IN ({placeholders})",
            list(beatport_ids),
        ).fetchall()
    by_bid: dict[int, dict] = {int(r["beatport_id"]): dict(r) for r in rows}
    return [by_bid[bp] for bp in beatport_ids if bp in by_bid]
