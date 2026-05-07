"""Browser-based SoundCloud fetch for content the OAuth API can't reach.

SoundCloud's `/discover/sets/personalized-tracks::user:id` URLs only exist on
the internal `api-v2.soundcloud.com` host, which doesn't accept OAuth bearer
tokens (every Authorization variant returns 403). The web app accesses them
via a logged-in browser session cookie.

We avoid asking you to log in twice by importing your existing SoundCloud
session cookies from Brave's cookie store (read-only, works while Brave is
running) and injecting them into a fresh Playwright context. Playwright
navigates to the URL, the page issues its api-v2 XHR using the imported
session, and our response listener captures the playlist payload.
"""
from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import async_playwright

from .brave_cookies import read_cookies_for_domain


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _fetch_personalized_async(url: str, headless: bool, timeout_s: int) -> dict:
    """Open `url` with imported Brave cookies, capture the api-v2 XHR payload."""
    cookies = read_cookies_for_domain("soundcloud.com")
    if not cookies:
        raise RuntimeError(
            "No SoundCloud cookies found in Brave. Open Brave, log in to "
            "soundcloud.com once, then re-run."
        )

    captured: dict[str, Any] = {}
    captured_event = asyncio.Event()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        try:
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1440, "height": 900},
            )
            await context.add_cookies(cookies)

            async def on_response(response):
                # Two response shapes carry the playlist content:
                #   (a) /system-playlists/<urn> or /playlists/<id> →
                #       dict with `tracks: [{...}]`
                #   (b) /tracks?ids=ID1,ID2,... (Discover-page batch fetch) →
                #       JSON array of track objects
                try:
                    if "api-v2.soundcloud.com" not in response.url:
                        return
                    if response.status != 200:
                        return
                    url_l = response.url
                    is_pl = "/system-playlists/" in url_l or "/playlists/" in url_l
                    is_batch = "/tracks" in url_l and "ids=" in url_l
                    if not (is_pl or is_batch):
                        return
                    body = await response.json()
                except Exception:
                    return

                if is_pl and isinstance(body, dict):
                    tracks = body.get("tracks")
                    if isinstance(tracks, list) and len(tracks) > 0:
                        captured["payload"] = body
                        captured_event.set()
                        return

                if is_batch and isinstance(body, list) and len(body) >= 5:
                    # Confirm it's a list of full track objects, not stubs
                    if all(isinstance(t, dict) and "title" in t for t in body):
                        captured["payload"] = {"tracks": body}
                        captured_event.set()

            page = await context.new_page()
            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            try:
                await asyncio.wait_for(captured_event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "Timed out waiting for SoundCloud to load the playlist. "
                    "Make sure you're logged in to SoundCloud in Brave, the URL "
                    "is correct, and the page actually shows tracks."
                )
        finally:
            await browser.close()

    return captured["payload"]


def fetch_personalized_set(
    url: str, headless: bool = True, timeout_s: int = 60
) -> dict:
    """Open `url` in a Playwright Chromium with cookies imported from Brave.

    Headless by default since cookies handle auth — no interactive login needed.
    """
    return asyncio.run(_fetch_personalized_async(url, headless, timeout_s))
