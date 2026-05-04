"""Radio.garden stream capture utilities."""

from __future__ import annotations

import re
import subprocess

import httpx

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def resolve_station(radio_garden_url: str) -> tuple[str, str]:
    """Parse a radio.garden URL, follow its redirect, and return (stream_url, station_name).

    radio.garden URLs:  https://radio.garden/listen/{slug}/{channel_id}
    Their API endpoint redirects to the real Icecast/SHOUTcast stream URL.
    We resolve that redirect here so ffmpeg gets a direct, stable URL.
    """
    m = re.search(r"/listen/([^/?#]+)/([^/?#]+)", radio_garden_url)
    if not m:
        raise ValueError(
            f"Cannot parse radio.garden URL: {radio_garden_url!r}\n"
            "Expected format: https://radio.garden/listen/<slug>/<channel-id>"
        )
    slug, channel_id = m.group(1), m.group(2)
    station_name = slug.replace("-", " ").title()

    api_url = f"https://radio.garden/api/ara/content/listen/{channel_id}/channel.mp3"
    # HEAD follows all redirects without downloading the stream body.
    with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": _UA}) as client:
        resp = client.head(api_url)
        stream_url = str(resp.url)

    return stream_url, station_name


def capture_chunk(stream_url: str, duration: int, dest: str) -> None:
    """Capture `duration` seconds of audio from a live stream into `dest` (MP3).

    Blocks for approximately `duration` real-world seconds because the source
    is a live radio stream — ffmpeg reads at 1× speed.

    Raises subprocess.CalledProcessError with .stderr populated on failure.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-user_agent", _UA,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", stream_url,
            "-t", str(duration),
            "-ar", "44100",
            "-ac", "2",
            "-q:a", "2",
            dest,
        ],
        capture_output=True,
        timeout=duration + 30,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )


def slice_audio(src: str, start: int, duration: int, dest: str) -> None:
    """Extract a sub-clip from a local audio file — runs faster than real time."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-t", str(duration),
            "-i", src,
            "-ar", "44100",
            "-ac", "2",
            "-q:a", "2",
            dest,
        ],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
