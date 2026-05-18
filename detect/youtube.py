"""YouTube video download and metadata extraction via yt-dlp."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_YTDLP = [sys.executable, "-m", "yt_dlp"]
_STATE_DIR = Path.home() / "Music/dj-tools/state"
_WORKING_BROWSER_FILE = _STATE_DIR / "yt_browser.txt"
_BROWSER_TTL = 7 * 24 * 3600
_BROWSERS = ["brave", "chrome", "safari", "firefox"]
_BOT_SENTINEL = "Sign in to confirm you're not a bot"


def _initial_cookie_flags() -> list[str]:
    """Return flags for the cached working browser, or the first browser in order."""
    if _WORKING_BROWSER_FILE.exists():
        age = time.time() - _WORKING_BROWSER_FILE.stat().st_mtime
        if age < _BROWSER_TTL:
            return ["--cookies-from-browser", _WORKING_BROWSER_FILE.read_text().strip()]
    return ["--cookies-from-browser", _BROWSERS[0]]


def _next_cookie_flags(current_flags: list[str]) -> list[str] | None:
    """Return flags for the next browser to try after current_flags, or None if exhausted."""
    current = current_flags[1] if len(current_flags) >= 2 else ""
    try:
        idx = _BROWSERS.index(current)
    except ValueError:
        idx = -1
    if idx + 1 < len(_BROWSERS):
        return ["--cookies-from-browser", _BROWSERS[idx + 1]]
    return None


def _save_working_browser(flags: list[str]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _WORKING_BROWSER_FILE.write_text(flags[1])


def _run_ytdlp(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found — install it with: pip install yt-dlp")


def resolve_video(url: str) -> tuple[str, str, int]:
    """Return (video_title, uploader, duration_seconds) without downloading.

    Raises RuntimeError if yt-dlp is missing or the URL is unresolvable.
    """
    cookie_flags = _initial_cookie_flags()
    while True:
        cmd = [*_YTDLP, "--dump-json", "--no-playlist", *cookie_flags, url]
        result = _run_ytdlp(cmd, timeout=30)
        if result.returncode == 0:
            _save_working_browser(cookie_flags)
            break
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp failed"
        if _BOT_SENTINEL in result.stderr:
            next_flags = _next_cookie_flags(cookie_flags)
            if next_flags:
                cookie_flags = next_flags
                continue
            raise RuntimeError(
                "YouTube bot detection on all browsers — sign in to YouTube in "
                "Brave, Chrome, Safari, or Firefox and retry"
            )
        raise RuntimeError(msg)

    info = json.loads(result.stdout)
    title = info.get("title") or info.get("fulltitle") or "Unknown Video"
    uploader = info.get("uploader") or info.get("channel") or ""
    duration = int(info.get("duration") or 0)
    return title, uploader, duration


def download_video(url: str, dest_dir: str) -> Path:
    """Download a YouTube video as MP3 into dest_dir; return the file path.

    Raises RuntimeError on failure.
    """
    out_template = str(Path(dest_dir) / "video.%(ext)s")
    cookie_flags = _initial_cookie_flags()
    while True:
        cmd = [
            *_YTDLP, "--no-playlist",
            *cookie_flags,
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3", "--audio-quality", "2",
            "-o", out_template,
            url,
        ]
        result = _run_ytdlp(cmd, timeout=7200)
        if result.returncode == 0:
            _save_working_browser(cookie_flags)
            break
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "download failed"
        if _BOT_SENTINEL in result.stderr:
            next_flags = _next_cookie_flags(cookie_flags)
            if next_flags:
                cookie_flags = next_flags
                continue
            raise RuntimeError(
                "YouTube bot detection on all browsers — sign in to YouTube in "
                "Brave, Chrome, Safari, or Firefox and retry"
            )
        raise RuntimeError(msg)

    candidate = Path(dest_dir) / "video.mp3"
    if candidate.exists():
        return candidate

    mp3_files = sorted(Path(dest_dir).glob("*.mp3"))
    if not mp3_files:
        raise RuntimeError(f"No MP3 found in {dest_dir} after download")
    return mp3_files[0]


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
