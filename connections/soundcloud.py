"""SoundCloud OAuth client (client_credentials flow).

Used by `detect/soundcloud.py` for set + single-track metadata, sidestepping
yt-dlp's flat-playlist limitations and rate limits. Public read-only access
only — no user content modification.

Token cache lives at `~/Music/dj-tools/state/soundcloud_token.json` (~1 hr TTL,
auto-refreshes on 401). Credentials are read from `SOUNDCLOUD_CLIENT_ID` +
`SOUNDCLOUD_CLIENT_SECRET` in `.env`. When credentials are absent, callers
fall back to yt-dlp + URL-slug derivation.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from paths import STATE_DIR


_AUTH_URL = "https://api.soundcloud.com/oauth2/token"
_API_BASE = "https://api.soundcloud.com"
_TOKEN_FILE = STATE_DIR / "soundcloud_token.json"
_TOKEN_REFRESH_BUFFER_S = 60  # refresh if <60s of token life remaining


class SoundCloudError(RuntimeError):
    """Raised on auth failure or unrecoverable API error."""


def has_credentials() -> bool:
    """True if both SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET are set."""
    return bool(
        os.environ.get("SOUNDCLOUD_CLIENT_ID")
        and os.environ.get("SOUNDCLOUD_CLIENT_SECRET")
    )


def _load_cached_token() -> str | None:
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("expires_at", 0) < time.time() + _TOKEN_REFRESH_BUFFER_S:
        return None
    return data.get("access_token")


def _save_token(token: str, expires_in: int) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps({
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }))
    _TOKEN_FILE.chmod(0o600)


def _fetch_new_token() -> str:
    client_id = os.environ.get("SOUNDCLOUD_CLIENT_ID")
    client_secret = os.environ.get("SOUNDCLOUD_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SoundCloudError(
            "SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET not set in .env. "
            "Either add them, or remove them entirely to use the slug-derived "
            "fallback path."
        )
    resp = httpx.post(
        _AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SoundCloudError(
            f"SoundCloud auth failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    body = resp.json()
    token = body.get("access_token")
    expires_in = body.get("expires_in", 3600)
    if not token:
        raise SoundCloudError(f"SoundCloud auth response missing access_token: {body}")
    _save_token(token, expires_in)
    return token


def _get_token() -> str:
    cached = _load_cached_token()
    return cached if cached else _fetch_new_token()


def _request(path: str, params: dict | None = None) -> Any:
    """Authenticated GET against api.soundcloud.com.

    Auto-retries once on 401 after refreshing the token (covers cached-but-stale).
    """
    url = f"{_API_BASE}{path}"

    def _call(token: str) -> httpx.Response:
        return httpx.get(
            url,
            params=params or {},
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
            follow_redirects=True,
        )

    resp = _call(_get_token())
    if resp.status_code == 401:
        if _TOKEN_FILE.exists():
            try:
                _TOKEN_FILE.unlink()
            except OSError:
                pass
        resp = _call(_fetch_new_token())

    if resp.status_code >= 400:
        raise SoundCloudError(
            f"SoundCloud API error: GET {path} → HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


def resolve_url(url: str) -> dict:
    """Resolve any soundcloud.com URL to its API object.

    Returns a dict with at least `kind` (`track` | `playlist` | `user` | `album`)
    and `id`. Raises SoundCloudError on auth/API failure.
    """
    return _request("/resolve", {"url": url})


def get_playlist_tracks(playlist_id: int) -> list[dict]:
    """Return all tracks in a playlist with full metadata.

    `/playlists/{id}` sometimes returns partial track stubs (just `id` + `kind`)
    for tracks beyond the first few. We batch-fetch missing full data via
    `/tracks?ids=` in chunks of 50 (SoundCloud's stated max).
    """
    pl = _request(f"/playlists/{playlist_id}")
    tracks = pl.get("tracks", [])

    missing_ids = [t["id"] for t in tracks if "title" not in t]
    if missing_ids:
        full_by_id: dict[int, dict] = {}
        for i in range(0, len(missing_ids), 50):
            chunk = missing_ids[i : i + 50]
            full = _request("/tracks", {"ids": ",".join(map(str, chunk))})
            for t in full:
                full_by_id[t["id"]] = t
        tracks = [full_by_id.get(t["id"], t) for t in tracks]

    return tracks
