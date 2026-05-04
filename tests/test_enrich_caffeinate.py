"""Tests that run_enrich uses caffeinate when tracks exist, not when there are none."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import detect.enrich as enrich_mod


@pytest.fixture
def mock_caffeinate():
    entered = []

    @contextmanager
    def _fake():
        entered.append(True)
        yield
        entered.append(False)

    with patch("detect.enrich.caffeinate", _fake):
        yield entered


@pytest.fixture
def mock_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich_mod, "_LOG_DIR", tmp_path)


def _make_run_enrich_stubs(tracks):
    """Return a stack of patches that let run_enrich execute without network I/O."""
    dummy_bp = MagicMock()
    dummy_bp.search_tracks.return_value = []

    return [
        patch("detect.enrich.detect_db.get_unenriched_tracks", return_value=tracks),
        patch("detect.enrich.detect_db.start_enrich_run", return_value=1),
        patch("detect.enrich.detect_db.finish_enrich_run"),
        patch("detect.enrich._get_token", return_value="Bearer fake"),
        patch("detect.enrich.bp_api.make_client", return_value=MagicMock()),
        patch("detect.enrich.bp_api.Beatport", return_value=dummy_bp),
    ]


def test_caffeinate_entered_when_tracks_exist(mock_caffeinate, mock_log_dir):
    track = {"id": 1, "artist": "Artist", "title": "Title"}
    patches = _make_run_enrich_stubs([track])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        enrich_mod.run_enrich(dry_run=False, limit=0, verbose=False,
                              threshold=0.72, retry_misses=False)
    assert mock_caffeinate == [True, False], "caffeinate should have been entered and exited"


def test_caffeinate_not_entered_when_no_tracks(mock_caffeinate, mock_log_dir):
    patches = _make_run_enrich_stubs([])
    with patches[0]:
        enrich_mod.run_enrich(dry_run=False, limit=0, verbose=False,
                              threshold=0.72, retry_misses=False)
    assert mock_caffeinate == [], "caffeinate must not run when there is nothing to enrich"
