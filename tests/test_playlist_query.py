"""Tests for playlist/query.py — runs against a temp DB with a minimal
enriched_tracks table populated by hand."""
from __future__ import annotations

import sqlite3

import pytest

import detect.db as ddb
from playlist import query as q


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(ddb, "DB_PATH", path)
    ddb.migrate()
    con = sqlite3.connect(path)
    con.execute("""
        INSERT INTO enriched_tracks
        (id, beatport_id, artist, title, bpm, genre, key, enriched_at)
        VALUES
        (1, 1001, 'Artist A', 'Title A', 124, 'Tech House', '5A',  '2026-01-01'),
        (2, 1002, 'Artist B', 'Title B', 128, 'Tech House', '7A',  '2026-01-01'),
        (3, 1003, 'Artist C', 'Title C', 132, 'Techno',     '9A',  '2026-01-01')
    """)
    con.commit()
    con.close()
    return path


# ── run_user_query ────────────────────────────────────────────────────────────

def test_returns_beatport_ids_in_query_order():
    bpids = q.run_user_query("SELECT beatport_id FROM enriched_tracks ORDER BY id")
    assert bpids == [1001, 1002, 1003]


def test_dedups_preserving_first_occurrence():
    bpids = q.run_user_query(
        "SELECT beatport_id FROM enriched_tracks "
        "UNION ALL SELECT beatport_id FROM enriched_tracks ORDER BY beatport_id"
    )
    assert bpids == [1001, 1002, 1003]


def test_skips_null_beatport_ids_from_outer_join():
    bpids = q.run_user_query("""
        SELECT et.beatport_id
        FROM (SELECT 'no-match' AS k) m
        LEFT JOIN enriched_tracks et ON et.beatport_id = -1
    """)
    assert bpids == []


def test_filtering_works():
    bpids = q.run_user_query(
        "SELECT beatport_id FROM enriched_tracks WHERE genre='Tech House' ORDER BY id"
    )
    assert bpids == [1001, 1002]


def test_rejects_non_select():
    with pytest.raises(ValueError, match="must start with SELECT"):
        q.run_user_query("DELETE FROM enriched_tracks")
    with pytest.raises(ValueError, match="must start with SELECT"):
        q.run_user_query("UPDATE enriched_tracks SET bpm=0")


def test_rejects_missing_beatport_id_column():
    with pytest.raises(ValueError, match="no 'beatport_id' column"):
        q.run_user_query("SELECT id, artist FROM enriched_tracks")


def test_returns_empty_when_no_rows_match():
    bpids = q.run_user_query(
        "SELECT beatport_id FROM enriched_tracks WHERE genre='Drum and Bass'"
    )
    assert bpids == []


def test_strips_leading_whitespace():
    bpids = q.run_user_query(
        "   \n  SELECT beatport_id FROM enriched_tracks WHERE id=1"
    )
    assert bpids == [1001]


def test_query_can_join_analysis_table():
    """User SQL can reference the analysis table even if no rows there yet —
    LEFT JOIN returns matches against enriched_tracks regardless."""
    bpids = q.run_user_query("""
        SELECT e.beatport_id FROM enriched_tracks e
        LEFT JOIN enriched_tracks_analysis a USING(beatport_id)
        WHERE e.bpm < 130
        ORDER BY e.id
    """)
    assert bpids == [1001, 1002]


# ── fetch_full_rows ───────────────────────────────────────────────────────────

def test_fetch_full_rows_preserves_input_order():
    rows = q.fetch_full_rows([1003, 1001, 1002])
    assert [r["beatport_id"] for r in rows] == [1003, 1001, 1002]
    assert rows[0]["genre"] == "Techno"
    assert rows[1]["bpm"] == 124


def test_fetch_full_rows_drops_unknown_ids():
    rows = q.fetch_full_rows([1001, 9999, 1002])
    assert [r["beatport_id"] for r in rows] == [1001, 1002]


def test_fetch_full_rows_empty_input():
    assert q.fetch_full_rows([]) == []


def test_fetch_full_rows_includes_analysis_when_present():
    """fetch_full_rows LEFT JOINs the analysis table, so analysis fields are
    None when no row exists, and populated when one does."""
    ddb.upsert_analysis(1001, {"mik_key": "8B", "mik_nrg": 7.5,
                               "vocals": "low", "drums": "high", "melody": "mid"})
    rows = q.fetch_full_rows([1001, 1002])
    assert rows[0]["mik_nrg"] == 7.5
    assert rows[0]["vocals"] == "low"
    assert rows[1]["mik_nrg"] is None
