"""Podbean episode metadata and download via direct page scrape — no yt-dlp."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx

# Full Chrome UA — Podbean redirects to the App Store with a truncated UA.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


def _fetch_html(url: str) -> str:
    """Fetch the Podbean episode page. Raises RuntimeError on HTTP failure."""
    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": _UA}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Podbean page: {exc}") from exc


def _meta(html: str, prop: str) -> str:
    """Extract og:<prop> meta tag content, handling both attribute orderings."""
    p = re.escape(prop)
    for pat in [
        rf'<meta\s[^>]*property=["\']og:{p}["\'][^>]*content=["\']([^"\']+)["\']',
        rf'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:{p}["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _podcast_name(html: str) -> str:
    """Extract podcast name from the <title> tag.

    Podbean title format: "{Podcast Name} Podcast - {Episode Title} | ..."
    """
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if not m:
        return ""
    pm = re.search(r"^(.+?)\s+Podcast\s*-", m.group(1), re.IGNORECASE)
    return pm.group(1).strip() if pm else ""


def _audio_url(html: str) -> str:
    """Extract the direct CDN audio URL from the episode page.

    Podbean embeds mcdn.podbean.com URLs directly in the page HTML.
    Prefer the /download/ path over /web/ for clean direct downloads.
    """
    for pattern in [
        r"https://mcdn\.podbean\.com/mf/download/[^\s\"'<>]+",
        r"https://mcdn\.podbean\.com/mf/web/[^\s\"'<>]+",
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(0)
    return ""


def resolve_episode(url: str) -> tuple[str, str, int]:
    """Return (episode_title, podcast_name, duration_seconds).

    Fetches the episode page via httpx and parses og:title + <title> tag.
    Duration is always 0 — determined by ffprobe after download.
    """
    html = _fetch_html(url)
    title = _meta(html, "title") or "Unknown Episode"
    podcast = _podcast_name(html)
    return title, podcast, 0


def download_episode(url: str, dest_dir: str) -> Path:
    """Fetch the episode page, extract the direct CDN audio URL, download via ffmpeg.

    Raises RuntimeError if the audio URL cannot be found or ffmpeg fails.
    """
    html = _fetch_html(url)
    audio_url = _audio_url(html)
    if not audio_url:
        raise RuntimeError(
            "Could not find audio URL on Podbean episode page — is this a valid episode URL?"
        )

    dest = str(Path(dest_dir) / "episode.mp3")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-user_agent", _UA,
            "-i", audio_url,
            "-ar", "44100",
            "-ac", "2",
            "-q:a", "2",
            dest,
        ],
        capture_output=True, text=True, timeout=7200,
    )
    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "ffmpeg download failed"
        raise RuntimeError(msg)
    return Path(dest)


def audio_duration(path: str) -> int:
    """Return the duration of an audio file in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        return int(float(result.stdout.strip()))
    return 0
