"""SoundCloud OAuth client (client_credentials + authorization_code flows).

Two auth modes coexist:

- **client_credentials** (default) — server-to-server, no user context. Works
  for public sets / tracks. Cached at `~/Music/dj-tools/state/soundcloud_token.json`.
- **authorization_code** (after `dj detect login-soundcloud`) — user-bound,
  required for personalized `/discover/sets/...` URLs and any user-private
  content. Cached at `~/Music/dj-tools/state/soundcloud_user_token.json` with a
  long-lived refresh token; auto-refreshed before expiry and on 401.

`_get_token()` prefers the user token when available and falls back to
client_credentials, so detect/soundcloud.py doesn't need to know which is
which.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from paths import STATE_DIR


_AUTH_URL = "https://api.soundcloud.com/oauth2/token"
_AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
_API_BASE = "https://api.soundcloud.com"
# Internal endpoint used by SoundCloud's web app — the only place /discover/
# personalized playlists are exposed. Accepts the same OAuth token api uses.
_API_V2_BASE = "https://api-v2.soundcloud.com"

_CLIENT_TOKEN_FILE = STATE_DIR / "soundcloud_token.json"
_USER_TOKEN_FILE = STATE_DIR / "soundcloud_user_token.json"
_TOKEN_REFRESH_BUFFER_S = 60  # refresh if <60s of token life remaining

# Backwards-compat alias — older code/tests reference this name.
_TOKEN_FILE = _CLIENT_TOKEN_FILE


class SoundCloudError(RuntimeError):
    """Raised on auth failure or unrecoverable API error."""


# ── Credentials / shared helpers ──────────────────────────────────────────────


def has_credentials() -> bool:
    """True if both SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET are set."""
    return bool(
        os.environ.get("SOUNDCLOUD_CLIENT_ID")
        and os.environ.get("SOUNDCLOUD_CLIENT_SECRET")
    )


def _require_credentials() -> tuple[str, str]:
    cid = os.environ.get("SOUNDCLOUD_CLIENT_ID")
    csecret = os.environ.get("SOUNDCLOUD_CLIENT_SECRET")
    if not cid or not csecret:
        raise SoundCloudError(
            "SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET not set in .env. "
            "Either add them, or remove them entirely to use the slug-derived "
            "fallback path."
        )
    return cid, csecret


def _save_token_file(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file, chmod to 0o600, then atomically replace the target
    # so the token is never briefly world-readable under the default umask.
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        os.write(fd, json.dumps(payload).encode())
        os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── client_credentials cache ──────────────────────────────────────────────────


def _load_cached_token() -> str | None:
    if not _CLIENT_TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_CLIENT_TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("expires_at", 0) < time.time() + _TOKEN_REFRESH_BUFFER_S:
        return None
    return data.get("access_token")


def _save_token(token: str, expires_in: int) -> None:
    _save_token_file(_CLIENT_TOKEN_FILE, {
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    })


def _fetch_new_token() -> str:
    cid, csecret = _require_credentials()
    resp = httpx.post(
        _AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": csecret,
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
        raise SoundCloudError(
            f"SoundCloud auth response missing access_token (keys: {list(body.keys())})"
        )
    _save_token(token, expires_in)
    return token


# ── authorization_code (user) flow ────────────────────────────────────────────


def has_user_auth() -> bool:
    """True if a user-bound OAuth token has been issued via login-soundcloud."""
    if not _USER_TOKEN_FILE.exists():
        return False
    try:
        data = json.loads(_USER_TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("refresh_token") or data.get("access_token"))


def _save_user_tokens(access_token: str, refresh_token: str, expires_in: int) -> dict:
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + int(expires_in),
    }
    _save_token_file(_USER_TOKEN_FILE, payload)
    return payload


def _load_user_token() -> dict | None:
    if not _USER_TOKEN_FILE.exists():
        return None
    try:
        return json.loads(_USER_TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _refresh_user_token(refresh_token: str) -> str | None:
    """POST refresh_token grant. On failure, drop the cache and return None."""
    cid, csecret = _require_credentials()
    resp = httpx.post(
        _AUTH_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": cid,
            "client_secret": csecret,
            "refresh_token": refresh_token,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(
            f"SoundCloud token refresh failed: HTTP {resp.status_code} — "
            f"{resp.text[:200]}. Clearing saved session.",
            file=sys.stderr,
        )
        try:
            _USER_TOKEN_FILE.unlink()
        except (OSError, FileNotFoundError):
            pass
        return None
    body = resp.json()
    access = body.get("access_token")
    if not access:
        return None
    # SoundCloud may rotate the refresh token; if it's not in the response,
    # keep the old one.
    new_refresh = body.get("refresh_token") or refresh_token
    _save_user_tokens(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=body.get("expires_in", 3600),
    )
    return access


def _get_user_access_token() -> str | None:
    data = _load_user_token()
    if not data:
        return None
    if data.get("expires_at", 0) >= time.time() + _TOKEN_REFRESH_BUFFER_S:
        return data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return None
    return _refresh_user_token(refresh_token)


# ── Browser-based login (one-shot) ────────────────────────────────────────────


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    expected_state: str | None = None

    def do_GET(self):  # noqa: N802 — http.server signature
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            # Verify state to guard against callback injection / CSRF
            if _CallbackHandler.expected_state:
                returned_state = params.get("state", [""])[0]
                if returned_state != _CallbackHandler.expected_state:
                    self._send(400, "<html><body><h1>Login failed</h1><p>State mismatch — possible CSRF. Try again.</p></body></html>")
                    return
            _CallbackHandler.code = params["code"][0]
            body = (
                "<html><body style='font-family: -apple-system, sans-serif; "
                "padding: 40px; max-width: 480px;'>"
                "<h1>SoundCloud login successful!</h1>"
                "<p>You can close this tab and return to your terminal.</p>"
                "</body></html>"
            )
            self._send(200, body)
        else:
            err = params.get("error", ["unknown"])[0]
            _CallbackHandler.error = err
            from html import escape as _html_escape
            self._send(400, f"<html><body><h1>Login failed</h1><p>{_html_escape(err)}</p></body></html>")

    def _send(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_: Any) -> None:  # silence default access-log
        return


def login_user(port: int = 8080) -> dict:
    """Run the OAuth authorization-code flow, save user tokens.

    Reads the redirect URI from `SOUNDCLOUD_REDIRECT_URI` (must exactly match
    one registered in the SoundCloud app); falls back to
    `http://localhost:{port}/callback`. Binds a one-shot HTTP server to the
    host/port parsed from that URI, opens the browser to SoundCloud's
    authorize URL, waits for the redirect with the auth code, exchanges the
    code for access + refresh tokens, and saves them. Returns the saved dict.
    """
    cid, csecret = _require_credentials()
    redirect_uri = os.environ.get(
        "SOUNDCLOUD_REDIRECT_URI", f"http://localhost:{port}/callback"
    )

    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    bind_port = parsed.port or port

    state = secrets.token_urlsafe(16)
    auth_url = _AUTHORIZE_URL + "?" + urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "non-expiring",
        "state": state,
    })

    # Reset class-level state so repeated logins work
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    _CallbackHandler.expected_state = state

    try:
        server = HTTPServer((host, bind_port), _CallbackHandler)
    except OSError as exc:
        raise SoundCloudError(
            f"Cannot bind {host}:{bind_port} for the OAuth callback server "
            f"(is something already using it?).\n"
            f"  ({exc})"
        )
    server.timeout = 1  # poll every second so the loop stays responsive

    print(f"Opening browser to authorize SoundCloud access…")
    print(f"  Callback URL: {redirect_uri}")
    print(f"  Local server: bound to {host}:{bind_port}")
    print(f"  (Make sure this exact URL is in your SoundCloud app's Redirect URI list.)")
    webbrowser.open(auth_url)

    # Loop until the code/error arrives.  Browsers prefetch /favicon.ico before
    # the real callback, which would consume a single handle_request() call and
    # leave the server without the auth code — so we keep looping until the
    # handler sets code or error.
    deadline = time.time() + 300
    while not (_CallbackHandler.code or _CallbackHandler.error):
        server.handle_request()
        if time.time() > deadline:
            break
    server.server_close()

    if _CallbackHandler.error:
        raise SoundCloudError(f"Authorization failed: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise SoundCloudError("Authorization timed out (no callback within 5 min).")

    code = _CallbackHandler.code
    resp = httpx.post(
        _AUTH_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "client_secret": csecret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SoundCloudError(
            f"Token exchange failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    body = resp.json()
    access = body.get("access_token")
    refresh = body.get("refresh_token", "")
    if not access:
        raise SoundCloudError(
            f"Token exchange response missing access_token (keys: {list(body.keys())})"
        )
    return _save_user_tokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=body.get("expires_in", 3600),
    )


# ── Token resolution + request dispatch ───────────────────────────────────────


def _get_token() -> str:
    """Return a valid access token, preferring user auth when available."""
    user_token = _get_user_access_token()
    if user_token:
        return user_token
    cached = _load_cached_token()
    return cached if cached else _fetch_new_token()


def _force_new_token(used_token: str) -> str:
    """Mint a fresh token after a 401 on `used_token`, preserving auth tier.

    Tries to refresh the user token (preserving the long-lived refresh_token)
    before falling back to client_credentials. Only drops a cache file if its
    refresh attempt itself fails — a transient 401 on a stale access_token
    must not destroy the refresh_token alongside it.
    """
    user_data = _load_user_token()
    if user_data and user_data.get("refresh_token"):
        # _refresh_user_token unlinks the user file on failure.
        new_user = _refresh_user_token(user_data["refresh_token"])
        if new_user and new_user != used_token:
            return new_user

    # Either no user auth, refresh failed, or refresh returned the same token.
    # Force a new client_credentials token.
    try:
        _CLIENT_TOKEN_FILE.unlink()
    except (OSError, FileNotFoundError):
        pass
    return _fetch_new_token()


def _request(path: str, params: dict | None = None) -> Any:
    """Authenticated GET. Auto-retries once on 401 after refreshing token."""
    url = f"{_API_BASE}{path}"

    def _call(token: str) -> httpx.Response:
        return httpx.get(
            url,
            params=params or {},
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
            follow_redirects=True,
        )

    token = _get_token()
    resp = _call(token)
    if resp.status_code == 401:
        resp = _call(_force_new_token(token))

    if resp.status_code >= 400:
        raise SoundCloudError(
            f"SoundCloud API error: GET {path} → HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


def resolve_url(url: str) -> dict:
    """Resolve any soundcloud.com URL to its API object."""
    return _request("/resolve", {"url": url})


def _request_v2(path: str, params: dict | None = None) -> Any:
    """GET against api-v2.soundcloud.com (internal — exposes /discover/ paths)."""
    url = f"{_API_V2_BASE}{path}"

    def _call(token: str) -> httpx.Response:
        return httpx.get(
            url,
            params=params or {},
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
            follow_redirects=True,
        )

    token = _get_token()
    resp = _call(token)
    if resp.status_code == 401:
        resp = _call(_force_new_token(token))

    if resp.status_code >= 400:
        raise SoundCloudError(
            f"SoundCloud api-v2 error: GET {path} → HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


def resolve_url_v2(url: str) -> dict:
    """Resolve via api-v2 — the only endpoint that exposes /discover/ URLs.

    api-v2 playlist responses inline the full track objects (no need to
    batch-fetch missing IDs separately).
    """
    return _request_v2("/resolve", {"url": url})


def get_playlist_tracks(playlist_id: int) -> list[dict]:
    """Return all tracks in a playlist with full metadata.

    `/playlists/{id}` sometimes returns partial track stubs (just `id` + `kind`)
    for tracks beyond the first few. We batch-fetch missing full data via
    `/tracks?ids=` in chunks of 50.
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
