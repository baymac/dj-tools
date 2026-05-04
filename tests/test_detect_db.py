"""Tests for detect/db.py — all operations use a temp DB to avoid touching dj.db."""
import sqlite3
from pathlib import Path

import pytest

import detect.db as db


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.migrate()
    return path


# ── Schema ────────────────────────────────────────────────────────────────────


def test_migrate_creates_tables(tmp_db):
    con = sqlite3.connect(tmp_db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"detected_tracks", "sessions", "track_sessions", "enriched_tracks",
            "beatport_playlists", "beatport_playlist_tracks", "enrich_runs",
            "deleted_sessions"} <= tables


def test_migrate_is_idempotent(tmp_db):
    db.migrate()
    db.migrate()


# ── Sessions ─────────────────────────────────────────────────────────────────


def test_create_session_returns_id(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=1", "Test Mix")
    assert isinstance(sid, int)
    assert sid > 0


def test_create_session_same_url_returns_same_id(tmp_db):
    a = db.create_session("youtube", "https://yt.com/v=1", "Test Mix")
    b = db.create_session("youtube", "https://yt.com/v=1", "Test Mix")
    assert a == b


def test_find_session(tmp_db):
    db.create_session("mixcloud", "https://mc.com/mix", "My Mix")
    row = db.find_session("https://mc.com/mix")
    assert row is not None
    assert row["type"] == "mixcloud"


def test_end_session(tmp_db):
    sid = db.create_session("radio", "https://r.example.com/", "Radio")
    db.end_session(sid)
    row = db.find_session("https://r.example.com/")
    assert row["ended_at"] is not None


def test_update_session_progress(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=2", "V2")
    db.update_session_progress(sid, 300)
    row = db.find_session("https://yt.com/v=2")
    assert row["last_scanned_position"] == 300


def test_list_sessions(tmp_db):
    db.create_session("youtube", "https://yt.com/v=3", "V3")
    db.create_session("youtube", "https://yt.com/v=4", "V4")
    rows = db.list_sessions("youtube")
    assert len(rows) == 2


# ── Tracks ────────────────────────────────────────────────────────────────────


def test_insert_track_returns_id(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=5", "V5")
    tid = db.insert_track(
        {"artist": "Bicep", "title": "Glue", "shazam_key": "SK1"},
        source="youtube", session_id=sid,
    )
    assert tid > 0


def test_insert_track_dedupes_by_shazam_key(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=6", "V6")
    t1 = db.insert_track({"artist": "A", "title": "T", "shazam_key": "SK2"}, source="youtube", session_id=sid)
    t2 = db.insert_track({"artist": "A", "title": "T", "shazam_key": "SK2"}, source="youtube", session_id=sid)
    assert t1 == t2


def test_insert_track_dedupes_by_artist_title(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=7", "V7")
    t1 = db.insert_track({"artist": "Bicep", "title": "Glue"}, source="youtube", session_id=sid)
    t2 = db.insert_track({"artist": "Bicep", "title": "Glue"}, source="youtube", session_id=sid)
    assert t1 == t2


def test_list_tracks(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=8", "V8")
    db.insert_track({"artist": "A", "title": "T1", "shazam_key": "SK3"}, source="youtube", session_id=sid)
    db.insert_track({"artist": "B", "title": "T2", "shazam_key": "SK4"}, source="youtube", session_id=sid)
    rows = db.list_tracks(10)
    assert len(rows) == 2


def test_tracks_for_session(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=9", "V9")
    db.insert_track({"artist": "A", "title": "T", "shazam_key": "SK5", "position": 60},
                    source="youtube", session_id=sid)
    rows = db.tracks_for_session(sid)
    assert len(rows) == 1
    assert rows[0]["position"] == 60


# ── delete_session ────────────────────────────────────────────────────────────


def test_delete_session_removes_orphan_tracks(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=del", "Del")
    db.insert_track({"artist": "Del", "title": "Track", "shazam_key": "SKDEL"},
                    source="youtube", session_id=sid)
    db.delete_session(sid)
    rows = db.list_tracks(10)
    assert len(rows) == 0


def test_delete_session_preserves_enriched_tracks(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=enrich", "Enrich")
    tid = db.insert_track({"artist": "X", "title": "Y", "shazam_key": "SKENRICH"},
                          source="youtube", session_id=sid)
    db.upsert_enriched(tid, {"beatport_id": 1234, "beatport_link": "https://bp.com/t/s/1234"})
    db.delete_session(sid)
    rows = db.list_tracks(10)
    assert len(rows) == 1  # track preserved because it has enrichment


def test_delete_session_not_in_null_bug(tmp_db):
    """Regression: NOT IN with NULLs must not protect unrelated tracks."""
    # Insert a beatport-synced track (detected_track_id IS NULL in enriched_tracks)
    db.upsert_beatport_playlist(99, "My BP Playlist")
    db.insert_beatport_track("BP Artist", "BP Track", "https://bp.com/t/s/999",
                             {"beatport_id": 999, "bpm": 128})

    # Now create a session with an unrelated track that has no enrichment
    sid = db.create_session("youtube", "https://yt.com/v=null_bug", "Bug")
    db.insert_track({"artist": "Unenriched", "title": "Track", "shazam_key": "SKNULL"},
                    source="youtube", session_id=sid)
    db.delete_session(sid)

    # The unenriched detected track should be gone
    rows = db.list_tracks(10)
    assert all(r["artist"] != "Unenriched" for r in rows)


# ── Enrichment ────────────────────────────────────────────────────────────────


def test_get_unenriched_tracks(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=u", "U")
    db.insert_track({"artist": "A", "title": "T", "shazam_key": "SKU1"}, source="youtube", session_id=sid)
    rows = db.get_unenriched_tracks()
    assert len(rows) == 1


def test_upsert_enriched(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=e", "E")
    tid = db.insert_track({"artist": "Burial", "title": "Archangel", "shazam_key": "SKE1"},
                          source="youtube", session_id=sid)
    db.upsert_enriched(tid, {"beatport_id": 5678, "beatport_link": "https://bp.com/t/archangel/5678",
                              "bpm": 140.0, "key": "3A"})
    rows = db.get_unenriched_tracks()
    assert len(rows) == 0


def test_upsert_enriched_stores_artist_title(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=at", "AT")
    tid = db.insert_track({"artist": "Four Tet", "title": "Baby", "shazam_key": "SKAT"},
                          source="youtube", session_id=sid)
    db.upsert_enriched(tid, {"beatport_id": 100, "beatport_link": "https://bp.com/t/baby/100"})
    rows = db.list_enriched_tracks(10)
    assert rows[0]["artist"] == "Four Tet"
    assert rows[0]["title"] == "Baby"


def test_mark_enrich_miss(tmp_db):
    sid = db.create_session("youtube", "https://yt.com/v=miss", "Miss")
    tid = db.insert_track({"artist": "Unknown", "title": "Unknown", "shazam_key": "SKMISS"},
                          source="youtube", session_id=sid)
    db.mark_enrich_miss(tid, "not_found")
    rows = db.get_unenriched_tracks()
    assert len(rows) == 0  # filtered out by enrich_outcome IS NOT NULL


def test_enrich_run_lifecycle(tmp_db):
    run_id = db.start_enrich_run()
    db.finish_enrich_run(run_id, seen=10, found=7, not_found=2, fuzzy_miss=1)
    runs = db.list_enrich_runs()
    assert runs[0]["status"] == "done"
    assert runs[0]["found"] == 7


# ── Beatport playlist sync ────────────────────────────────────────────────────


def test_upsert_beatport_playlist_returns_id(tmp_db):
    pid = db.upsert_beatport_playlist(42, "Tech House")
    assert pid > 0


def test_upsert_beatport_playlist_idempotent(tmp_db):
    a = db.upsert_beatport_playlist(42, "Tech House")
    b = db.upsert_beatport_playlist(42, "Tech House")
    assert a == b


def test_insert_beatport_track_new(tmp_db):
    pid = db.upsert_beatport_playlist(1, "Playlist A")
    acted = db.insert_beatport_track(
        "Bicep", "Glue", "https://bp.com/t/glue/1111",
        {"beatport_id": 1111, "bpm": 133.0, "key": "5A"},
        playlist_id=pid,
    )
    assert acted is True


def test_insert_beatport_track_duplicate_same_playlist(tmp_db):
    pid = db.upsert_beatport_playlist(1, "Playlist A")
    db.insert_beatport_track("Bicep", "Glue", "https://bp.com/t/glue/1111",
                             {"beatport_id": 1111}, playlist_id=pid)
    acted = db.insert_beatport_track("Bicep", "Glue", "https://bp.com/t/glue/1111",
                                     {"beatport_id": 1111}, playlist_id=pid)
    assert acted is False  # already linked


def test_insert_beatport_track_two_playlists(tmp_db):
    p1 = db.upsert_beatport_playlist(1, "Playlist A")
    p2 = db.upsert_beatport_playlist(2, "Playlist B")
    db.insert_beatport_track("Bicep", "Glue", "https://bp.com/t/glue/1111",
                             {"beatport_id": 1111}, playlist_id=p1)
    acted = db.insert_beatport_track("Bicep", "Glue", "https://bp.com/t/glue/1111",
                                     {"beatport_id": 1111}, playlist_id=p2)
    assert acted is True  # new playlist link = new event
    # Still only one enriched_tracks row
    rows = db.list_enriched_tracks(10)
    assert len(rows) == 1


def test_list_enriched_tracks_playlist_filter(tmp_db):
    p1 = db.upsert_beatport_playlist(1, "TechHouse")
    p2 = db.upsert_beatport_playlist(2, "Melodic")
    db.insert_beatport_track("A", "T1", "https://bp.com/t/t1/1", {"beatport_id": 1}, playlist_id=p1)
    db.insert_beatport_track("B", "T2", "https://bp.com/t/t2/2", {"beatport_id": 2}, playlist_id=p2)

    tech = db.list_enriched_tracks(10, playlist_name="TechHouse")
    assert len(tech) == 1
    assert tech[0]["title"] == "T1"


def test_get_studio_enrichable_tracks(tmp_db):
    pid = db.upsert_beatport_playlist(1, "PL")
    db.insert_beatport_track("A", "T", "https://bp.com/t/t/10", {"beatport_id": 10}, playlist_id=pid)
    rows = db.get_studio_enrichable_tracks()
    assert len(rows) == 1
    assert rows[0]["artist"] == "A"


def test_update_studio_enrich(tmp_db):
    pid = db.upsert_beatport_playlist(1, "PL")
    db.insert_beatport_track("A", "T", "https://bp.com/t/t/10", {"beatport_id": 10}, playlist_id=pid)
    rows = db.get_studio_enrichable_tracks()
    db.update_studio_enrich(rows[0]["id"], {"mik_key": "8B", "mik_nrg": 7.5, "vocals": "low", "drums": "high", "melody": "mid"})
    remaining = db.get_studio_enrichable_tracks()
    assert len(remaining) == 0
