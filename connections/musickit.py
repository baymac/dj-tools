"""Wrapper around the Swift musickit_bridge — auto-compile, run, and stream tracks."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

_BRIDGE_SRC = Path(__file__).resolve().parent / "bridge" / "musickit_bridge.swift"
_CACHE_DIR = Path.home() / ".cache" / "playlist-syncer"


def _bridge_binary() -> Path:
    """Compile musickit_bridge.swift if needed. Cache by source hash."""
    src = _BRIDGE_SRC
    src_hash = hashlib.md5(src.read_bytes()).hexdigest()[:8]
    binary = _CACHE_DIR / f"musickit_bridge_{src_hash}"
    if not binary.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for old in _CACHE_DIR.glob("musickit_bridge_*"):
            old.unlink(missing_ok=True)
        result = subprocess.run(
            ["swiftc", str(src), "-o", str(binary)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Swift compile failed:\n{result.stderr}")
    return binary


def run_bridge(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    binary = _bridge_binary()
    return subprocess.run(
        [str(binary)] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def check_musickit() -> tuple[bool, str]:
    """Return (authorized, message). Exit code 2 = not authorized."""
    try:
        result = run_bridge(["--check"], timeout=30)
        if result.returncode == 0:
            return True, "MusicKit authorized"
        if result.returncode == 2:
            return False, (
                "MusicKit not authorized.\n"
                "Open the Music app, then re-run this command to grant access."
            )
        return False, f"MusicKit check failed (exit {result.returncode}): {result.stderr.strip()}"
    except Exception as e:
        return False, f"MusicKit check error: {e}"


def list_playlists() -> list[str]:
    """Return Apple Music playlist names via MusicKit."""
    result = run_bridge(["--list-playlists"])
    if result.returncode != 0:
        raise RuntimeError(f"MusicKit error: {result.stderr.strip()}")
    return json.loads(result.stdout.strip())


def _stream_bridge(args: list[str]):
    """Run the MusicKit bridge with given args and yield track dicts from NDJSON stdout."""
    binary = _bridge_binary()
    proc = subprocess.Popen(
        [str(binary)] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        proc.stdout.close()

    exit_code = proc.wait()
    if exit_code != 0:
        stderr = proc.stderr.read()
        raise RuntimeError(
            f"MusicKit bridge exited with code {exit_code}. "
            f"Sync may be incomplete.\n{stderr.strip()}"
        )


def stream_playlist_tracks(playlist_name: str):
    yield from _stream_bridge(["--playlist", playlist_name])


def stream_library_tracks():
    """Yield track dicts for songs with libraryAddedDate set (Music app 'Songs' tab)."""
    yield from _stream_bridge(["--library-songs"])


def stream_favorite_tracks():
    """Yield track dicts for songs in the 'Favourite Songs' playlist."""
    yield from _stream_bridge(["--favorites"])


def stream_library_and_favorites_tracks():
    """Yield track dicts for songs that are in library OR in Favourite Songs."""
    yield from _stream_bridge(["--library-and-favorites"])


def stream_all_tracks():
    """Yield all track dicts from MusicLibraryRequest<Song> with no filter."""
    yield from _stream_bridge([])
