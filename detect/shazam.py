"""Shazam audio recognition and Apple Music ID extraction."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*audioop.*", category=DeprecationWarning)

from shazamio import Shazam

RECOGNIZE_TIMEOUT = 30  # seconds before giving up on a Shazam API call


async def recognize_file(path: str) -> dict:
    """
    Recognize the track in an audio or video file.
    Videos are converted to a short MP3 clip via ffmpeg before sending to Shazam.
    Raises asyncio.TimeoutError if Shazam does not respond within RECOGNIZE_TIMEOUT seconds.
    """
    audio_path = path
    tmp_file = None

    if Path(path).suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_file.close()
        audio_path = tmp_file.name
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", path,
                "-t", "30",       # first 30 s is enough for Shazam
                "-ar", "44100",
                "-ac", "2",
                "-q:a", "2",
                audio_path,
            ],
            capture_output=True,
            check=True,
        )

    try:
        shazam = Shazam()
        result = await asyncio.wait_for(shazam.recognize(audio_path), timeout=RECOGNIZE_TIMEOUT)
        return result
    finally:
        if tmp_file:
            Path(audio_path).unlink(missing_ok=True)


def _apple_music_url(hub: dict) -> str | None:
    """Pull the Apple Music https URL from a Shazam hub dict."""
    for option in hub.get("options", []):
        for action in option.get("actions", []):
            uri = action.get("uri", "")
            if uri.startswith("https://music.apple.com"):
                return uri
    # Fallback: bare actions list
    for action in hub.get("actions", []):
        uri = action.get("uri", "")
        if uri.startswith("https://music.apple.com"):
            return uri
    return None


def _apple_music_id(hub: dict) -> str | None:
    """Return the numeric Apple Music track ID from a Shazam hub dict."""
    for action in hub.get("actions", []):
        if action.get("type") == "applemusicplay":
            return action.get("id")
    return None


def format_result(raw: dict) -> dict:
    """Convert a raw Shazam response into a clean track dict."""
    track = raw.get("track") or {}
    if not track:
        return {}

    hub = track.get("hub") or {}
    am_id = _apple_music_id(hub)
    am_url = _apple_music_url(hub)

    # If no explicit URL was found, build one from the ID
    if am_id and not am_url:
        am_url = f"https://music.apple.com/song/{am_id}"

    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "apple_music_id": am_id,
        "apple_music_url": am_url,
        "shazam_key": track.get("key", ""),
    }
