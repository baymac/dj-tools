"""Beatport HTTP API client and token capture."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright

API_ROOT = "https://api.beatport.com/v4"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------- Auth ----------

from paths import BROWSER_PROFILE_DIR as _BROWSER_PROFILE_PATH
_BROWSER_PROFILE = str(_BROWSER_PROFILE_PATH)

# Real browser executables on macOS — using the actual binary avoids Cloudflare's
# headless-Chromium fingerprint detection.
_BROWSER_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _find_real_browser() -> Optional[str]:
    import os as _os
    for path in _BROWSER_CANDIDATES:
        if _os.path.exists(path):
            return path
    return None


async def _capture_session_cookie_async(
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
) -> str:
    """Open browser, return __Secure-next-auth.session-token cookie.

    If already logged in (persistent profile), grabs cookie immediately.
    Only fills the login form if it appears AND username+password are provided.
    """
    exe = _find_real_browser()
    args = ["--no-sandbox"]
    if not headless:
        args += ["--window-size=1440,900"]

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            _BROWSER_PROFILE,
            headless=headless,
            args=args,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            **({"executable_path": exe} if exe else {}),
        )
        for p in context.pages:
            await p.close()
        page = await context.new_page()
        await page.goto("https://www.beatport.com/", wait_until="domcontentloaded")
        try:
            await page.wait_for_function(
                "() => !document.title.includes('Just a moment')",
                timeout=15000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        try:
            await page.locator("#onetrust-accept-btn-handler").click(timeout=2000)
        except Exception:
            pass
        await page.goto("https://account.beatport.com/settings", wait_until="domcontentloaded")
        # Wait for Cloudflare challenge to auto-resolve (title changes from "Just a moment...")
        try:
            await page.wait_for_function(
                "() => !document.title.includes('Just a moment')",
                timeout=15000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        login_form_visible = await page.locator("input[name='username']").count() > 0
        if login_form_visible:
            if username and password:
                await page.fill("input[name='username']", username)
                await page.fill("input[name='password']", password)
                await page.click("button[type='submit']")
                await page.wait_for_timeout(3000)
            elif not headless:
                # Visible browser — let user log in interactively
                print("\n>>> Log in to Beatport in the browser window that just opened.")
                print(">>> This window will close automatically once you're logged in.\n")
            else:
                raise RuntimeError(
                    "Beatport login form appeared but no credentials provided.\n"
                    "Set BEATPORT_USERNAME and BEATPORT_PASSWORD in .env, or use --ui\n"
                    "to log in interactively."
                )
        await page.goto("https://www.beatport.com/library/playlists", wait_until="domcontentloaded")
        try:
            await page.wait_for_function(
                "() => !document.title.includes('Just a moment')",
                timeout=15000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        # Dismiss cookie consent modal if it reappears
        try:
            await page.locator("#onetrust-accept-btn-handler").click(timeout=2000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        # Dismiss any generic close-button modal
        for sel in ["button[aria-label='Close']", "button[aria-label='close']", "[data-testid='modal-close']"]:
            try:
                await page.locator(sel).click(timeout=1000)
                await page.wait_for_timeout(500)
            except Exception:
                pass
        await page.wait_for_timeout(2000)

        def _find_session_cookie(cookies: list) -> Optional[str]:
            for c in cookies:
                if c.get("name") == "__Secure-next-auth.session-token":
                    return c.get("value")
            return None

        session_cookie = _find_session_cookie(await context.cookies())

        # Visible browser: poll until session cookie appears (user may still be logging in)
        if not session_cookie and not headless:
            print("Waiting for Beatport login… (up to 2 minutes)")
            for _ in range(60):
                await page.wait_for_timeout(2000)
                session_cookie = _find_session_cookie(await context.cookies())
                if session_cookie:
                    break

        await context.close()

    if not session_cookie:
        raise RuntimeError(
            "Beatport login failed — session cookie not found.\n"
            "Check BEATPORT_USERNAME and BEATPORT_PASSWORD.\n"
            "If Cloudflare blocks headless mode, retry with --ui flag."
        )
    return session_cookie



def capture_token(username: Optional[str] = None, password: Optional[str] = None, headless: bool = True) -> tuple[str, str]:
    """Browser login → (access_token, session_cookie).

    Gets session cookie via browser, then uses refresh_via_session to get the
    access token. NextAuth rotates the session cookie on every refresh, and
    refresh_via_session persists the rotated cookie to .env. We read it back
    here so the returned cookie is the one currently valid server-side, not
    the (now-invalidated) one we captured from the browser.
    """
    session_cookie = asyncio.run(_capture_session_cookie_async(username, password, headless=headless))
    access_token = refresh_via_session(session_cookie)
    if not access_token:
        raise RuntimeError(
            "Logged in but failed to get access token from /api/auth/session.\n"
            "The session may not have been fully established — try again."
        )

    try:
        from dotenv import dotenv_values
        env_path = __import__("pathlib").Path(__file__).resolve().parent.parent / ".env"
        current = dotenv_values(str(env_path)).get("BEATPORT_SESSION_TOKEN") or session_cookie
    except Exception:
        current = session_cookie
    return access_token, current


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split()[-1].split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


_NEXTAUTH_SESSION_URL = "https://www.beatport.com/api/auth/session"


def refresh_via_session(session_cookie: str, *, verbose: bool = False) -> Optional[str]:
    """Refresh the Beatport access token using the NextAuth session cookie.

    Calls /api/auth/session with the __Secure-next-auth.session-token cookie.
    NextAuth uses the embedded refresh token server-side and returns a new accessToken.
    NextAuth ALSO rotates the session cookie on every refresh — if a new cookie comes
    back in Set-Cookie, we persist it to .env so the next refresh works. Otherwise
    the next call would use a stale cookie and fail.

    Returns 'Bearer <new_token>' or None if refresh failed or returned an expired token.
    Set verbose=True (or BEATPORT_DEBUG=1 in the env) to print the real cause to stderr.
    """
    import os
    if os.environ.get("BEATPORT_DEBUG"):
        verbose = True

    def _why(msg: str) -> None:
        if verbose:
            print(f"[refresh_via_session] {msg}", file=sys.stderr)

    try:
        r = httpx.get(
            _NEXTAUTH_SESSION_URL,
            headers={
                "cookie": f"__Secure-next-auth.session-token={session_cookie}",
                "user-agent": USER_AGENT,
            },
            timeout=15,
            follow_redirects=True,
        )
    except Exception as e:
        _why(f"HTTP request failed: {type(e).__name__}: {e}")
        return None

    if r.status_code != 200:
        _why(f"HTTP {r.status_code}: {r.text[:200]!r}")
        return None

    try:
        data = r.json()
    except Exception as e:
        _why(f"JSON parse failed: {e}; body head={r.text[:200]!r}")
        return None

    token_data = data.get("token") or {}
    err = token_data.get("error")
    if err:
        _why(f"NextAuth returned token.error={err!r} — server-side refresh chain is broken. "
             f"Wipe ~/Music/dj-tools/state/browser-profile/ and re-run `dj login-beatport --ui`.")
        return None

    new_token = token_data.get("accessToken")
    if not new_token:
        _why(f"no accessToken in response; token keys={list(token_data.keys())}")
        return None

    bearer = f"Bearer {new_token}"
    if _jwt_payload(bearer).get("exp", 0) <= time.time():
        _why("accessToken returned but already expired by JWT exp")
        return None

    rotated_cookie = r.cookies.get("__Secure-next-auth.session-token")
    if rotated_cookie and rotated_cookie != session_cookie:
        save_token_to_env(bearer, rotated_cookie)
    else:
        save_token_to_env(bearer)
    return bearer


def save_token_to_env(token: str, session_cookie: Optional[str] = None) -> None:
    """Persist token (and optionally session cookie) back to .env."""
    try:
        from dotenv import set_key
        env_path = __import__("pathlib").Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            set_key(str(env_path), "BEATPORT_ACCESS_TOKEN", token.removeprefix("Bearer ").strip())
            if session_cookie:
                set_key(str(env_path), "BEATPORT_SESSION_TOKEN", session_cookie)
    except Exception:
        pass


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        timeout=30,
        headers={
            "authorization": token,
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "user-agent": USER_AGENT,
            "origin": "https://www.beatport.com",
            "referer": "https://www.beatport.com/",
        },
    )


# ---------- API client ----------

class AuthExpiredError(Exception):
    """Raised when a Beatport token is expired and cannot be refreshed."""


@dataclass
class Beatport:
    client: httpx.Client
    on_401: Optional[Callable[[], None]] = field(default=None)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(6):
            r = self.client.request(method, url, **kw)
            if r.status_code == 429:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
            elif r.status_code == 401 and self.on_401 and attempt == 0:
                self.on_401()
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r  # unreachable

    def get_track(self, track_id: int) -> Optional[dict]:
        """GET /catalog/tracks/{id}/ — full track record including sample_url."""
        try:
            return self._request(
                "GET", f"{API_ROOT}/catalog/tracks/{track_id}/"
            ).json()
        except AuthExpiredError:
            raise
        except Exception:
            return None

    def preview_url(self, track_id: int) -> Optional[str]:
        """Return the 30s preview MP3 URL for a track, or None."""
        rec = self.get_track(track_id)
        if not rec:
            return None
        return (
            rec.get("sample_url")
            or rec.get("sample_mp3_url")
            or (rec.get("sample") or {}).get("url")
        )

    def search_tracks(
        self, query: str, per_page: int = 5, debug: bool = False
    ) -> Optional[list[dict]]:
        """Search catalog.
        Returns list of track dicts (possibly empty), or None if request failed.
        Empty list = genuinely no results. None = request error (retry next run).
        """
        try:
            data = self._request(
                "GET",
                f"{API_ROOT}/catalog/search/",
                params={"q": query, "type": "tracks", "page": 1, "per_page": per_page},
            ).json()
            if isinstance(data, list):
                tracks = data
            else:
                tracks_raw = data.get("tracks", [])
                tracks = tracks_raw if isinstance(tracks_raw, list) else tracks_raw.get("data", [])
        except AuthExpiredError:
            raise
        except Exception as e:
            if debug:
                print(f"[search primary] {query!r}: {type(e).__name__}: {e}", file=sys.stderr)
            return None

        if tracks:
            return tracks

        try:
            data = self._request(
                "GET",
                f"{API_ROOT}/catalog/tracks/",
                params={"q": query, "page": 1, "per_page": per_page},
            ).json()
            if isinstance(data, list):
                return data
            return data.get("results", [])
        except AuthExpiredError:
            raise
        except Exception as e:
            if debug:
                print(f"[search fallback] {query!r}: {type(e).__name__}: {e}", file=sys.stderr)
            return None


    def list_my_playlists(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            data = self._request(
                "GET", f"{API_ROOT}/my/playlists/?page={page}&per_page=50"
            ).json()
            out.extend(data["results"])
            if not data.get("next"):
                break
            page += 1
        return out

    def create_playlist(self, name: str) -> dict:
        return self._request(
            "POST",
            f"{API_ROOT}/my/playlists/",
            json={"name": name},
        ).json()

    def list_track_ids(self, playlist_id: int) -> set[int]:
        try:
            data = self._request(
                "GET", f"{API_ROOT}/my/playlists/{playlist_id}/tracks/ids/"
            ).json()
            if "results" in data:
                return {item.get("track_id") or item.get("id") for item in data["results"]}
            if "track_ids" in data:
                return set(data["track_ids"])
        except Exception:
            pass
        return self._list_track_ids_paged(playlist_id)

    def _list_track_ids_paged(self, playlist_id: int) -> set[int]:
        ids: set[int] = set()
        page = 1
        while True:
            data = self._request(
                "GET",
                f"{API_ROOT}/my/playlists/{playlist_id}/tracks/"
                f"?page={page}&per_page=100",
            ).json()
            for entry in data["results"]:
                tid = entry.get("track_id") or entry.get("track", {}).get("id")
                if tid:
                    ids.add(tid)
            if not data.get("next"):
                break
            page += 1
        return ids

    def list_playlist_items(self, playlist_id: int) -> list[dict]:
        """Return raw playlist track entries, each containing item `id` and catalog `track_id`."""
        items: list[dict] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"{API_ROOT}/my/playlists/{playlist_id}/tracks/",
                params={"page": page, "per_page": 100},
            ).json()
            items.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        return items

    def add_track(self, dest_id: int, track_id: int) -> dict:
        return self._request(
            "POST",
            f"{API_ROOT}/my/playlists/{dest_id}/tracks/bulk/",
            json={"track_ids": [track_id]},
        ).json()

    def delete_track(self, playlist_id: int, track_id: int) -> None:
        """Remove a track from a playlist using its internal playlist item ID."""
        items = self.list_playlist_items(playlist_id)
        item_id: Optional[int] = None
        for item in items:
            catalog_id = item.get("track_id") or item.get("track", {}).get("id")
            if catalog_id == track_id:
                item_id = item.get("id")
                break

        if item_id is None:
            raise ValueError(
                f"Track {track_id} not found in playlist {playlist_id}."
            )

        self._request(
            "DELETE",
            f"{API_ROOT}/my/playlists/{playlist_id}/tracks/bulk/",
            json={"item_ids": [item_id]},
        )
