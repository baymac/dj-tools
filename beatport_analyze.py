#!/usr/bin/env python3
"""
Analyse Beatport tracks for key and energy using DJ Studio's bundled MIK engine.

Usage:
    uv run beatport_analyze.py <url> [url ...]          # analyse and print
    uv run beatport_analyze.py <url> [url ...] --import  # also store in track_db

Examples:
    uv run beatport_analyze.py https://www.beatport.com/track/some-track/18398870
    uv run beatport_analyze.py https://www.beatport.com/track/foo/111 https://... --import
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent
DJS_ANALYZE = SCRIPT_DIR / "djs_analyze.js"
CHENNAI_DB  = Path.home() / "conductor/workspaces/beatport/chennai/state/sync.db"
DJ_LIBRARY  = Path.home() / "Music/DJ.Studio/Database/audio-library-table"
TRACK_DB    = Path.home() / "Music/DJ.Studio/track_metadata.db"

API_ROOT = "https://api.beatport.com/v4"

# Beatport key.id → Camelot (matches CAMELOT_MAP in track_db.py, 1-indexed)
# from https://api.beatport.com/v4/catalog/keys/
BP_KEY_TO_CAMELOT: dict[int, str] = {
    1:"1A",2:"2A",3:"3A",4:"4A",5:"5A",6:"6A",7:"7A",8:"8A",
    9:"9A",10:"10A",11:"11A",12:"12A",
    13:"1B",14:"2B",15:"3B",16:"4B",17:"5B",18:"6B",19:"7B",20:"8B",
    21:"9B",22:"10B",23:"11B",24:"12B",
}

# ── Beatport API ───────────────────────────────────────────────────────────────

def _load_token() -> str | None:
    if not CHENNAI_DB.exists():
        return None
    con = sqlite3.connect(str(CHENNAI_DB))
    row = con.execute(
        "SELECT token FROM auth_cache WHERE service='beatport'"
    ).fetchone()
    con.close()
    return row[0] if row else None


def _api_get(path: str, token: str) -> dict:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    req = urllib.request.Request(
        url,
        headers={"authorization": token, "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_track(track_id: int, token: str) -> dict:
    """Fetch full track metadata from Beatport API."""
    return _api_get(f"/catalog/tracks/{track_id}/", token)


def parse_track_id(url: str) -> int:
    """Extract numeric track ID from a Beatport URL."""
    m = re.search(r"/(\d+)/?$", url.rstrip("/"))
    if not m:
        raise ValueError(f"Cannot parse track ID from: {url}")
    return int(m.group(1))


# ── Audio analysis ─────────────────────────────────────────────────────────────

def download_preview(sample_url: str, dest: Path) -> None:
    """Download preview MP3 (no auth needed — public CDN)."""
    with urllib.request.urlopen(sample_url, timeout=30) as r:
        dest.write_bytes(r.read())


def decode_to_pcm(mp3_path: Path, pcm_path: Path) -> None:
    """Convert MP3 → 44100 Hz mono float32-LE PCM via ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-i", str(mp3_path), "-ac", "1", "-ar", "44100",
         "-f", "f32le", "-y", str(pcm_path)],
        check=True, capture_output=True,
    )


def run_djs_analysis(pcm_path: Path) -> dict:
    """Detect BPM via DJ Studio's ai-beatgrid addon and key via chromagram analysis.

    Returns {key, camelotKey, mikKeyNr, bpm}. Key uses Krumhansl-Schmuckler profiles
    on librosa's CQT chromagram; BPM from the ai-beatgrid Node addon.
    """
    import numpy as np
    import librosa

    raw = pcm_path.read_bytes()
    samples = np.frombuffer(raw, dtype="<f4")

    key_display = _estimate_key(samples, sr=44100)
    key_idx = KEY_MAP.get(key_display, -1)
    camelot = CAMELOT[key_idx] if key_idx >= 0 else None
    mik_key_nr = key_idx + 1 if key_idx >= 0 else None
    bpm = _run_djs_bpm(pcm_path)

    return {"key": key_display, "camelotKey": camelot, "mikKeyNr": mik_key_nr, "bpm": bpm}


def _estimate_key(samples, sr: int = 44100) -> str:
    """Krumhansl-Schmuckler key detection on a CQT chromagram."""
    import numpy as np
    import librosa

    # Krumhansl-Schmuckler pitch-class profiles (major / minor)
    _KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    _KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

    chroma = librosa.feature.chroma_cqt(y=samples, sr=sr)
    mean_chroma = chroma.mean(axis=1)

    best_key, best_mode, best_corr = 0, "major", -2.0
    for i in range(12):
        for profile, mode in [(_KS_MAJOR, "major"), (_KS_MINOR, "minor")]:
            rotated = np.roll(profile, i)
            corr = float(np.corrcoef(mean_chroma, rotated)[0, 1])
            if corr > best_corr:
                best_corr, best_key, best_mode = corr, i, mode

    _PITCH_NAMES = ["C", "D flat", "D", "E flat", "E", "F",
                    "G flat", "G", "A flat", "A", "B flat", "B"]
    return f"{_PITCH_NAMES[best_key]} {best_mode}"


# Matches djs_analyze.js KEY_MAP (0-indexed → CAMELOT index)
KEY_MAP: dict[str, int] = {
    "C major": 0, "D flat major": 1, "D major": 2, "E flat major": 3,
    "E major": 4, "F major": 5, "G flat major": 6, "G major": 7,
    "A flat major": 8, "A major": 9, "B flat major": 10, "B major": 11,
    "A minor": 12, "B flat minor": 13, "B minor": 14, "C minor": 15,
    "D flat minor": 16, "D minor": 17, "E flat minor": 18, "E minor": 19,
    "F minor": 20, "F sharp minor": 21, "G minor": 22, "A flat minor": 23,
}

CAMELOT: list[str] = [
    "8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B",
    "5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A",
]


def _run_djs_bpm(pcm_path: Path) -> int | None:
    """Call djs_analyze.js → {bpm}. Returns None on failure."""
    if not DJS_ANALYZE.exists():
        return None
    try:
        result = subprocess.run(
            ["node", str(DJS_ANALYZE), str(pcm_path)],
            capture_output=True, text=True, check=True, timeout=60,
        )
        data = json.loads(result.stdout.strip())
        return data.get("bpm") or None
    except Exception:
        return None


def compute_energy(pcm_path: Path) -> int:
    """Derive a 1-10 energy score from RMS loudness of the PCM audio.

    Maps the typical electronic music dynamic range (-30 to -10 dBFS) to 1-10.
    Louder / more compressed = higher energy, quieter / more dynamic = lower.
    """
    raw = pcm_path.read_bytes()
    n = len(raw) // 4
    if n == 0:
        return 5
    samples = struct.unpack_from(f"<{n}f", raw)
    rms = math.sqrt(sum(s * s for s in samples) / n)
    if rms <= 0:
        return 1
    db = 20 * math.log10(rms)          # dBFS  (full scale = 0)
    # -10 dBFS → 10,  -30 dBFS → 1
    score = (db - (-30)) / ((-10) - (-30)) * 9 + 1
    return max(1, min(10, round(score)))


# ── DJ Studio library lookup ───────────────────────────────────────────────────

def lookup_dj_studio(track_id: int) -> dict | None:
    """Return the DJ Studio audio-library-table entry for a Beatport track, if present."""
    library_key = f"beatport-sdk_{track_id}"
    for shard in DJ_LIBRARY.iterdir():
        if not shard.is_dir():
            continue
        candidate = shard / library_key
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                return None
    return None


# ── track_db import ────────────────────────────────────────────────────────────

def _camelot_map() -> dict[int, str]:
    return {
        1:"8B",2:"3B",3:"10B",4:"5B",5:"12B",6:"7B",
        7:"2B",8:"9B",9:"4B",10:"11B",11:"6B",12:"1B",
        13:"5A",14:"12A",15:"7A",16:"2A",17:"9A",18:"4A",
        19:"11A",20:"6A",21:"1A",22:"8A",23:"3A",24:"10A",
    }


def import_to_track_db(track_id: int, bp: dict, analysis: dict, energy: int) -> None:
    """Upsert analysis results into track_metadata.db."""
    import datetime, re as _re
    CMAP = _camelot_map()
    library_key = f"beatport-sdk_{track_id}"
    title   = bp.get("name", "Unknown")
    artists = bp.get("artists", [])
    artist  = ", ".join(a["name"] for a in artists) if artists else "Unknown"
    genre   = bp.get("genre", {}).get("name", "") if isinstance(bp.get("genre"), dict) else ""
    bpm     = analysis.get("bpm") or bp.get("bpm")
    key     = analysis.get("camelotKey")
    mik_key_nr = analysis.get("mikKeyNr")

    bp_key  = bp.get("key", {})
    bp_camelot = None
    if isinstance(bp_key, dict):
        bp_camelot = f"{bp_key.get('camelot_number','')}{bp_key.get('camelot_letter','')}".strip()

    slug    = title.lower()
    slug    = _re.sub(r"[^\w\s-]", "", slug)
    slug    = _re.sub(r"[\s_]+", "-", slug).strip("-")
    bp_url  = f"https://www.beatport.com/track/{slug}/{track_id}"

    if not TRACK_DB.exists():
        print("  track_db not found — run: uv run track_db.py populate first", file=sys.stderr)
        return

    now = datetime.datetime.now().isoformat()
    con = sqlite3.connect(str(TRACK_DB))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("""
        INSERT INTO tracks
            (library_key, beatport_id, beatport_url, title, artist, genre, key, bpm,
             energy, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(library_key) DO UPDATE SET
            title=excluded.title, artist=excluded.artist, genre=excluded.genre,
            beatport_url=COALESCE(tracks.beatport_url, excluded.beatport_url),
            key=COALESCE(tracks.key, excluded.key),
            bpm=COALESCE(tracks.bpm, excluded.bpm),
            energy=COALESCE(tracks.energy, excluded.energy),
            updated_at=excluded.updated_at
    """, (library_key, str(track_id), bp_url, title, artist, genre,
          key, bpm, energy, now, now))
    con.commit()
    con.close()
    print(f"  Stored in track_db: {library_key}")


# ── Main ───────────────────────────────────────────────────────────────────────

def analyse_url(url: str, token: str, do_import: bool) -> None:
    track_id = parse_track_id(url)
    print(f"\n{'─'*60}")
    print(f"Fetching track {track_id}...")

    bp = fetch_track(track_id, token)
    title   = bp.get("name", "?")
    artists = bp.get("artists", [])
    artist  = ", ".join(a["name"] for a in artists) if artists else "?"
    bpm_bp  = bp.get("bpm", "?")
    key_bp  = bp.get("key", {})
    bp_camelot = (
        f"{key_bp['camelot_number']}{key_bp['camelot_letter']}"
        if isinstance(key_bp, dict) and "camelot_number" in key_bp
        else "?"
    )
    sample_url = bp.get("sample_url")
    print(f"{artist} — {title}  (BPM: {bpm_bp}, Beatport key: {bp_camelot})")

    # Check if already in DJ Studio
    djs = lookup_dj_studio(track_id)
    if djs:
        mik_nr = djs.get("mikKey") or djs.get("camelotKey")
        from track_db import camelot_key as _ck
        existing_key = _ck(mik_nr) or "?"
        print(f"  Already in DJ Studio: key={existing_key}  energy={djs.get('mikEnergy','?')}")
        if not do_import:
            return

    if not sample_url:
        print("  No preview URL available — skipping analysis.")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        mp3 = tmp / "preview.mp3"
        pcm = tmp / "preview.f32"

        print(f"  Downloading preview...")
        download_preview(sample_url, mp3)

        print(f"  Decoding audio...")
        decode_to_pcm(mp3, pcm)

        print(f"  Detecting key (chromagram) and BPM (ai-beatgrid)...")
        analysis = run_djs_analysis(pcm)

        print(f"  Computing energy...")
        energy = compute_energy(pcm)

    key_mik = analysis.get("camelotKey", "?")
    bpm_mik = analysis.get("bpm", "?")

    print(f"\n  ┌─ Results ─────────────────────────────")
    print(f"  │  Key (MIK):      {key_mik}  ← {analysis.get('key','')}")
    print(f"  │  Key (Beatport): {bp_camelot}")
    print(f"  │  BPM (MIK):      {bpm_mik}")
    print(f"  │  BPM (Beatport): {bpm_bp}")
    print(f"  │  Energy (1-10):  {energy}")
    print(f"  └───────────────────────────────────────")

    if do_import:
        import_to_track_db(track_id, bp, analysis, energy)


def main():
    parser = argparse.ArgumentParser(
        description="Analyse Beatport tracks using DJ Studio's MIK engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run beatport_analyze.py https://www.beatport.com/track/title/18398870
  uv run beatport_analyze.py <url1> <url2> --import
        """,
    )
    parser.add_argument("urls", nargs="+", help="Beatport track URLs")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="Store results in track_metadata.db")
    args = parser.parse_args()

    if not shutil.which("node"):
        print("Error: node not found. Install Node.js.", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("ffmpeg"):
        print("Error: ffmpeg not found. Install with: brew install ffmpeg", file=sys.stderr)
        sys.exit(1)
    if not DJS_ANALYZE.exists():
        print(f"Error: {DJS_ANALYZE} not found.", file=sys.stderr)
        sys.exit(1)

    token = _load_token()
    if not token:
        print("Error: No Beatport token found. Run the chennai sync tool to log in first.",
              file=sys.stderr)
        sys.exit(1)

    for url in args.urls:
        try:
            analyse_url(url, token, args.do_import)
        except Exception as e:
            print(f"  Error analysing {url}: {e}", file=sys.stderr)

    print()


if __name__ == "__main__":
    main()
