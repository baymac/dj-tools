"""Tests for sync/db.py — all operations use a temp DB."""
import sqlite3
from pathlib import Path

import pytest

import sync.db as sdb


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "sync_test.db"
    monkeypatch.setattr(sdb, "DB_PATH", path)
    sdb.init_db(path)
    return path


def test_init_db_creates_tables(tmp_db):
    con = sqlite3.connect(tmp_db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"synced_tracks", "sync_runs", "auth_cache", "cursors"} <= tables


def test_init_db_idempotent(tmp_db):
    sdb.init_db(tmp_db)
    sdb.init_db(tmp_db)


def test_mark_synced_and_load(tmp_db):
    sdb.mark_synced("cat123", "library", "added",
                    beatport_track_id=9999, dest_playlist="Tech House",
                    db_path=tmp_db)
    synced = sdb.load_synced_set("library", db_path=tmp_db)
    assert "cat123" in synced


def test_load_synced_set_only_terminal_outcomes(tmp_db):
    # "added" is terminal
    sdb.mark_synced("a", "library", "added", db_path=tmp_db)
    # "fuzzy_miss" is terminal
    sdb.mark_synced("b", "library", "fuzzy_miss", db_path=tmp_db)
    # "pending" is NOT terminal
    sdb.mark_synced("c", "library", "pending", db_path=tmp_db)
    synced = sdb.load_synced_set("library", db_path=tmp_db)
    assert "a" in synced
    assert "b" in synced
    assert "c" not in synced


def test_load_synced_set_scoped_to_playlist(tmp_db):
    sdb.mark_synced("x", "library", "added", db_path=tmp_db)
    sdb.mark_synced("y", "favorites", "added", db_path=tmp_db)
    lib = sdb.load_synced_set("library", db_path=tmp_db)
    fav = sdb.load_synced_set("favorites", db_path=tmp_db)
    assert "x" in lib and "y" not in lib
    assert "y" in fav and "x" not in fav


def test_sync_run_lifecycle(tmp_db):
    run_id = sdb.start_sync_run("library", db_path=tmp_db)
    sdb.finish_sync_run(run_id, 10, 7, 2, 1, status="done", db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    con.close()
    assert row[8] == "done"  # status column


def test_cursor_get_set(tmp_db):
    assert sdb.get_cursor("library_cursor", db_path=tmp_db) is None
    sdb.set_cursor("library_cursor", "2024-01-01T00:00:00", db_path=tmp_db)
    assert sdb.get_cursor("library_cursor", db_path=tmp_db) == "2024-01-01T00:00:00"


def test_cursor_overwrite(tmp_db):
    sdb.set_cursor("k", "v1", db_path=tmp_db)
    sdb.set_cursor("k", "v2", db_path=tmp_db)
    assert sdb.get_cursor("k", db_path=tmp_db) == "v2"


def test_mark_synced_replace_on_duplicate(tmp_db):
    sdb.mark_synced("dup", "library", "added", beatport_track_id=1, db_path=tmp_db)
    sdb.mark_synced("dup", "library", "duplicate", db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    rows = con.execute("SELECT outcome FROM synced_tracks WHERE catalog_id = 'dup'").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][0] == "duplicate"
