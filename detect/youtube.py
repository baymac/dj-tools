"""YouTube video download and metadata extraction via yt-dlp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def resolve_video(url: str) -> tuple[str, str, int]:
    """Return (video_title, uploader, duration_seconds) without downloading.

    Raises RuntimeError if yt-dlp is missing or the URL is unresolvable.
    """
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found — install it with: pip install yt-dlp")

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp failed"
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
    cmd = [
        "yt-dlp", "--no-playlist",
        "-x", "--audio-format", "mp3", "--audio-quality", "2",
        "-o", out_template,
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found — install it with: pip install yt-dlp")

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "download failed"
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
