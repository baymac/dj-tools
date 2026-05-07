"""Tests for connections/soundcloud.py — auth flow, no real network calls."""
import json
import time

import pytest

import connections.soundcloud as sc


@pytest.fixture(autouse=True)
def isolate_token_cache(tmp_path, monkeypatch):
    """Point the token cache at a temp file so tests don't touch real state."""
    monkeypatch.setattr(sc, "_TOKEN_FILE", tmp_path / "soundcloud_token.json")
    return tmp_path


def test_has_credentials_true_when_both_set(monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_ID", "id")
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_SECRET", "secret")
    assert sc.has_credentials() is True


def test_has_credentials_false_when_either_missing(monkeypatch):
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_SECRET", "secret")
    assert sc.has_credentials() is False

    monkeypatch.setenv("SOUNDCLOUD_CLIENT_ID", "id")
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_SECRET", raising=False)
    assert sc.has_credentials() is False


def test_fetch_token_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_SECRET", raising=False)
    with pytest.raises(sc.SoundCloudError, match="not set in .env"):
        sc._fetch_new_token()


def test_load_cached_token_returns_none_when_missing():
    assert sc._load_cached_token() is None


def test_load_cached_token_returns_valid_token():
    sc._save_token("test-token-123", expires_in=3600)
    assert sc._load_cached_token() == "test-token-123"


def test_load_cached_token_returns_none_when_expired():
    # Save a token with past expiry
    sc._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc._TOKEN_FILE.write_text(json.dumps({
        "access_token": "stale",
        "expires_at": int(time.time()) - 100,
    }))
    assert sc._load_cached_token() is None


def test_load_cached_token_refreshes_within_buffer():
    """Tokens with <60s of life left should be treated as expired (refresh buffer)."""
    sc._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc._TOKEN_FILE.write_text(json.dumps({
        "access_token": "almost-stale",
        "expires_at": int(time.time()) + 30,  # 30s remaining < 60s buffer
    }))
    assert sc._load_cached_token() is None


def test_save_token_writes_owner_only_perms():
    sc._save_token("xyz", expires_in=1000)
    assert sc._TOKEN_FILE.exists()
    # 0o600 = read/write for owner, nothing for group/other
    assert sc._TOKEN_FILE.stat().st_mode & 0o777 == 0o600
