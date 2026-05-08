"""Tests for connections/soundcloud.py — auth flow, no real network calls."""
import json
import time

import pytest

import connections.soundcloud as sc


@pytest.fixture(autouse=True)
def isolate_token_cache(tmp_path, monkeypatch):
    """Point both token cache files at a temp dir so tests don't touch real state."""
    client_path = tmp_path / "soundcloud_token.json"
    user_path = tmp_path / "soundcloud_user_token.json"
    monkeypatch.setattr(sc, "_TOKEN_FILE", client_path)
    monkeypatch.setattr(sc, "_CLIENT_TOKEN_FILE", client_path)
    monkeypatch.setattr(sc, "_USER_TOKEN_FILE", user_path)
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


# ── User auth (authorization_code) ────────────────────────────────────────────


def test_has_user_auth_false_when_file_missing():
    assert sc.has_user_auth() is False


def test_has_user_auth_true_when_refresh_token_present():
    sc._save_user_tokens(access_token="a", refresh_token="r", expires_in=3600)
    assert sc.has_user_auth() is True


def test_save_user_tokens_writes_owner_only_perms():
    sc._save_user_tokens(access_token="a", refresh_token="r", expires_in=3600)
    assert sc._USER_TOKEN_FILE.stat().st_mode & 0o777 == 0o600


def test_get_user_access_token_returns_cached_when_valid():
    sc._save_user_tokens(access_token="user-token", refresh_token="r", expires_in=3600)
    assert sc._get_user_access_token() == "user-token"


def test_get_user_access_token_returns_none_when_missing():
    assert sc._get_user_access_token() is None


def test_get_token_prefers_user_over_client_credentials():
    sc._save_user_tokens(access_token="USER", refresh_token="r", expires_in=3600)
    sc._save_token("CLIENT", expires_in=3600)
    assert sc._get_token() == "USER"


def test_get_token_falls_back_to_client_when_no_user_auth():
    sc._save_token("CLIENT", expires_in=3600)
    assert sc._get_token() == "CLIENT"
