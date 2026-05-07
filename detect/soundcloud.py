"""SoundCloud mix download via yt-dlp.

SoundCloud is yt-dlp-supported out of the box (no auth needed for public mixes).
Mirrors `detect/youtube.py` shape so `_run_soundcloud` can drop in alongside
`_run_youtube` in cli.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# SoundCloud share-links carry tracking params (`?si=...&utm_*=...`) that drift
# per share. We canonicalize so two scans of the same mix hit the same session.
_TRACKING_PARAMS = {"si", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}

# "Artist - Title" / "Artist – Title" / "Artist — Title" — first separator wins.
_TITLE_SEP_RE = re.compile(r"\s+[-–—]\s+")


def clean_url(url: str) -> str:
    """Strip SoundCloud's share/tracking query params for stable session keys."""
    parts = urlparse(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    return urlunparse(parts._replace(query=urlencode(kept)))


def is_set_url(url: str) -> bool:
    """True if `url` is a SoundCloud set (multi-track playlist).

    Sets are handled in cli.py via the metadata-enumeration path (`list_set_tracks`)
    rather than the audio-scan path used for single mixes.
    """
    return "/sets/" in urlparse(url).path


def parse_artist_title(raw_title: str, fallback_uploader: str = "") -> tuple[str, str]:
    """Split a SoundCloud upload title into (artist, title).

    Most uploads use 'Artist - Title' or 'Artist – Title'. When no separator
    is present, falls back to (uploader, full_title).
    """
    title = (raw_title or "").strip()
    m = _TITLE_SEP_RE.search(title)
    if m:
        artist = title[: m.start()].strip()
        track_title = title[m.end() :].strip()
        if artist and track_title:
            return artist, track_title
    return (fallback_uploader or "").strip() or "Unknown Artist", title or "Unknown Title"


def derive_from_url(track_url: str) -> tuple[str, str]:
    """Best-effort (artist, title) derivation from a SoundCloud track URL.

    URL shape: `https://soundcloud.com/<uploader>/<track-slug>`. We use the
    uploader handle as artist and the slug as title (with `-`/`_` → space and
    title-cased). Used as a fallback when yt-dlp's `--flat-playlist` mode
    omits per-entry title/uploader fields (it does for SoundCloud sets), so
    enrichment has *something* to fuzzy-match against Beatport.
    """
    parts = urlparse(track_url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return "Unknown Artist", "Unknown Title"
    uploader_slug, track_slug = segments[0], segments[-1]
    artist = uploader_slug.replace("_", " ").replace("-", " ").strip().title() or "Unknown Artist"
    title = track_slug.replace("_", " ").replace("-", " ").strip().title() or "Unknown Title"
    return artist, title


def list_set_tracks(url: str) -> list[dict]:
    """Enumerate tracks in a SoundCloud set without downloading audio.

    Prefers the SoundCloud OAuth API when SOUNDCLOUD_CLIENT_ID / _SECRET are
    configured (one call, full per-track metadata). Otherwise falls back to
    yt-dlp `--flat-playlist` plus URL-slug derivation (best-effort).

    Returns one dict per track: `{position, artist, title, source_url, duration}`.
    """
    try:
        from connections import soundcloud as sc_api
        if sc_api.has_credentials():
            resolved = sc_api.resolve_url(url)
            kind = resolved.get("kind")
            if kind in ("playlist", "system-playlist"):
                api_tracks = sc_api.get_playlist_tracks(resolved["id"])
                return [_format_oauth_track(t, i) for i, t in enumerate(api_tracks, 1)]
            # Not a playlist — let caller fall through to yt-dlp (rare; e.g. /albums/)
    except sc_api.SoundCloudError as exc:
        # Auth-configured but failing — surface the real error rather than
        # silently dropping to yt-dlp (which would hide a config problem).
        raise RuntimeError(str(exc))

    return _list_set_tracks_via_ytdlp(url)


def _format_oauth_track(api_track: dict, position: int) -> dict:
    """SoundCloud /tracks API response → our internal track dict."""
    raw_title = api_track.get("title") or ""
    user = api_track.get("user") or {}
    uploader = user.get("username") or ""
    artist, title = parse_artist_title(raw_title, uploader)
    duration_ms = api_track.get("duration") or 0
    return {
        "position": position,
        "artist": artist,
        "title": title,
        "source_url": api_track.get("permalink_url") or "",
        "duration": int(duration_ms // 1000),
    }


def _list_set_tracks_via_ytdlp(url: str) -> list[dict]:
    cmd = ["yt-dlp", "--flat-playlist", "--dump-json", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found — install it with: pip install yt-dlp")

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp failed"
        raise RuntimeError(msg)

    tracks: list[dict] = []
    for pos, line in enumerate(result.stdout.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        info = json.loads(line)
        raw_title = info.get("title") or info.get("fulltitle") or ""
        uploader = (
            info.get("uploader") or info.get("uploader_id") or info.get("channel") or ""
        )
        track_url = info.get("webpage_url") or info.get("url") or ""
        # Flat-playlist for SoundCloud sets omits title/uploader fields; we
        # fall back to URL-slug derivation so enrichment has something to
        # fuzzy-match. Stage 3 (Beatport enrich) is the source of truth for
        # the real artist/title anyway.
        if raw_title or uploader:
            artist, title = parse_artist_title(raw_title, uploader)
        else:
            artist, title = derive_from_url(track_url)
        tracks.append({
            "position": pos,
            "artist": artist,
            "title": title,
            "source_url": track_url,
            "duration": int(info.get("duration") or 0),
        })
    return tracks


def resolve_mix(url: str) -> tuple[str, str, int]:
    """Return (title, uploader, duration_seconds) without downloading audio.

    Prefers the SoundCloud OAuth API when SOUNDCLOUD_CLIENT_ID / _SECRET are
    configured (cleaner metadata, no rate limits); falls back to yt-dlp scrape.

    Raises RuntimeError if both paths fail.
    """
    try:
        from connections import soundcloud as sc_api
        if sc_api.has_credentials():
            resolved = sc_api.resolve_url(url)
            if resolved.get("kind") == "track":
                title = resolved.get("title") or "Unknown Mix"
                user = resolved.get("user") or {}
                uploader = user.get("username") or ""
                duration = int((resolved.get("duration") or 0) // 1000)
                return title, uploader, duration
            # Resolved object isn't a track (e.g. /sets/) — let caller handle it.
            raise RuntimeError(
                f"SoundCloud URL resolved to '{resolved.get('kind')}' not 'track'"
            )
    except sc_api.SoundCloudError as exc:
        raise RuntimeError(str(exc))

    cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found — install it with: pip install yt-dlp")

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp failed"
        raise RuntimeError(msg)

    info = json.loads(result.stdout)
    title = info.get("title") or info.get("fulltitle") or "Unknown Mix"
    uploader = info.get("uploader") or info.get("uploader_id") or ""
    duration = int(info.get("duration") or 0)
    return title, uploader, duration


def download_mix(url: str, dest_dir: str) -> Path:
    """Download a SoundCloud mix as MP3 into dest_dir; return the file path."""
    out_template = str(Path(dest_dir) / "mix.%(ext)s")
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

    candidate = Path(dest_dir) / "mix.mp3"
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
