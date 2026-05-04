"""Tests for the caffeinate context manager in detect/import_to_studio.py."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from detect.import_to_studio import _caffeinate


@pytest.fixture
def mock_popen():
    proc = MagicMock()
    with patch("detect.import_to_studio.subprocess.Popen", return_value=proc) as p:
        yield p, proc


def test_caffeinate_starts_with_correct_args(mock_popen):
    popen, _ = mock_popen
    with _caffeinate():
        pass
    popen.assert_called_once_with(["caffeinate", "-i"], close_fds=True)


def test_caffeinate_terminates_on_normal_exit(mock_popen):
    _, proc = mock_popen
    with _caffeinate():
        pass
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once()


def test_caffeinate_terminates_on_exception(mock_popen):
    _, proc = mock_popen
    with pytest.raises(RuntimeError):
        with _caffeinate():
            raise RuntimeError("boom")
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once()


def test_caffeinate_active_during_body(mock_popen):
    popen, proc = mock_popen
    body_ran = []
    with _caffeinate():
        # Inside the body: Popen called, terminate not yet called
        assert popen.call_count == 1
        assert proc.terminate.call_count == 0
        body_ran.append(True)
    assert body_ran == [True]
    assert proc.terminate.call_count == 1
