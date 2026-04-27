"""Tests for trackdb commands using an in-memory SQLite database."""

import sqlite3
import sys
from argparse import Namespace
from unittest.mock import patch

import pytest

from trackdb.schema import init_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def _insert_track(conn, library_key="beatport-sdk_111", title="Track", artist="Artist"):
    conn.execute(
        """INSERT INTO tracks
               (library_key, beatport_id, beatport_url, title, artist, genre, key, bpm,
                energy, vocals, drums, melody, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2024-01-01', '2024-01-01')""",
        (library_key, "111", None, title, artist, "Techno", "6A", 130.0, 7, "low", "high", "medium"),
    )
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE library_key = ?", (library_key,)).fetchone()["id"]


# ── cmd_populate ──────────────────────────────────────────────────────────────

def test_cmd_populate_inserts_tracks(db):
    from trackdb.commands import cmd_populate

    lib_key = "beatport-sdk_99"
    library = {
        lib_key: {
            "key": lib_key,
            "tag": {"title": "My Track", "artist": "DJ X", "genre": "Techno"},
            "bpm": 128.0,
            "mikKey": 13,
            "mikEnergy": 7,
            "structureKey": lib_key,
        }
    }
    project = {"name": "Test Mix", "lastModified": "2024-01-01T00:00:00", "key": "abc123xyz"}

    with (
        patch("trackdb.commands.load_dj_studio_library", return_value=library),
        patch("trackdb.commands.mix_track_keys", return_value=({lib_key}, project)),
        patch("trackdb.commands.load_dj_studio_structures", return_value={}),
        patch("trackdb.commands.read_stem_intensities", return_value={"vocals": "low"}),
    ):
        cmd_populate(db, Namespace(mix_name="Test Mix"))

    row = db.execute("SELECT * FROM tracks WHERE library_key = ?", (lib_key,)).fetchone()
    assert row is not None
    assert row["title"] == "My Track"
    assert row["artist"] == "DJ X"
    assert row["bpm"] == 128.0
    assert row["energy"] == 7
    assert row["vocals"] == "low"
    assert row["key"] == "5A"  # mikKey=13 → CAMELOT_MAP[13] = "5A"


def test_cmd_populate_preserves_user_edits(db):
    """COALESCE logic: user-set energy/vocals/drums/melody survive re-populate."""
    from trackdb.commands import cmd_populate

    lib_key = "beatport-sdk_42"
    _insert_track(db, library_key=lib_key)
    db.execute("UPDATE tracks SET energy = 9, vocals = 'high' WHERE library_key = ?", (lib_key,))
    db.commit()

    library = {
        lib_key: {
            "key": lib_key,
            "tag": {"title": "Updated Title", "artist": "Artist", "genre": "House"},
            "bpm": 125.0,
            "mikKey": None,
            "mikEnergy": None,
            "structureKey": lib_key,
        }
    }
    project = {"name": "Mix", "lastModified": "2024-01-01T00:00:00", "key": "abc123xyz"}

    with (
        patch("trackdb.commands.load_dj_studio_library", return_value=library),
        patch("trackdb.commands.mix_track_keys", return_value=({lib_key}, project)),
        patch("trackdb.commands.load_dj_studio_structures", return_value={}),
        patch("trackdb.commands.read_stem_intensities", return_value={}),
    ):
        cmd_populate(db, Namespace(mix_name="Mix"))

    row = db.execute("SELECT * FROM tracks WHERE library_key = ?", (lib_key,)).fetchone()
    assert row["title"] == "Updated Title"  # metadata updated
    assert row["energy"] == 9               # user edit preserved
    assert row["vocals"] == "high"          # user edit preserved


def test_cmd_populate_sections_added_from_mik(db):
    from trackdb.commands import cmd_populate

    lib_key = "beatport-sdk_77"
    structures = {
        lib_key: {
            "energyLevelData": [
                {"nr": 0, "label": 3, "startBeatNr": 0, "beatLength": 32},
                {"nr": 1, "label": 8, "startBeatNr": 32, "beatLength": 64},
                {"nr": 2, "label": 3, "startBeatNr": 96, "beatLength": 32},
            ]
        }
    }
    library = {
        lib_key: {
            "key": lib_key,
            "tag": {"title": "T", "artist": "A", "genre": ""},
            "bpm": 130.0,
            "mikKey": None,
            "mikEnergy": None,
            "structureKey": lib_key,
        }
    }
    project = {"name": "S", "lastModified": "2024-01-01T00:00:00", "key": "abc123xyz"}

    with (
        patch("trackdb.commands.load_dj_studio_library", return_value=library),
        patch("trackdb.commands.mix_track_keys", return_value=({lib_key}, project)),
        patch("trackdb.commands.load_dj_studio_structures", return_value=structures),
        patch("trackdb.commands.read_stem_intensities", return_value={}),
    ):
        cmd_populate(db, Namespace(mix_name="S"))

    sections = db.execute(
        "SELECT section_type FROM track_sections ORDER BY start_beat"
    ).fetchall()
    types = [s["section_type"] for s in sections]
    assert types == ["intro", "drop", "outro"]


# ── cmd_update ────────────────────────────────────────────────────────────────

def test_cmd_update_sets_fields(db):
    from trackdb.commands import cmd_update

    _insert_track(db)
    args = Namespace(
        library_key="beatport-sdk_111",
        energy=9, vocals="high", drums=None, melody=None,
        notes="great drop", key=None, bpm=None, beatport_url=None,
    )
    cmd_update(db, args)

    row = db.execute("SELECT * FROM tracks WHERE library_key = 'beatport-sdk_111'").fetchone()
    assert row["energy"] == 9
    assert row["vocals"] == "high"
    assert row["notes"] == "great drop"


def test_cmd_update_exits_on_bad_energy(db):
    from trackdb.commands import cmd_update

    _insert_track(db)
    args = Namespace(
        library_key="beatport-sdk_111",
        energy=11, vocals=None, drums=None, melody=None,
        notes=None, key=None, bpm=None, beatport_url=None,
    )
    with pytest.raises(SystemExit):
        cmd_update(db, args)


# ── cmd_section_* ─────────────────────────────────────────────────────────────

def test_cmd_section_add_and_list(db, capsys):
    from trackdb.commands import cmd_section_add, cmd_section_list

    _insert_track(db)
    cmd_section_add(db, Namespace(
        library_key="beatport-sdk_111",
        type="drop", start_beat=64.0, end_beat=128.0, notes="main drop",
    ))

    cmd_section_list(db, Namespace(library_key="beatport-sdk_111"))
    out = capsys.readouterr().out
    assert "drop" in out
    assert "64.0" in out


def test_cmd_section_remove(db, capsys):
    from trackdb.commands import cmd_section_remove, cmd_section_add

    _insert_track(db)
    cmd_section_add(db, Namespace(
        library_key="beatport-sdk_111",
        type="breakdown", start_beat=32.0, end_beat=64.0, notes=None,
    ))

    section_id = db.execute("SELECT id FROM track_sections").fetchone()["id"]
    cmd_section_remove(db, Namespace(section_id=section_id))

    remaining = db.execute("SELECT COUNT(*) FROM track_sections").fetchone()[0]
    assert remaining == 0


def test_cmd_section_remove_missing_exits(db):
    from trackdb.commands import cmd_section_remove

    with pytest.raises(SystemExit):
        cmd_section_remove(db, Namespace(section_id=9999))
