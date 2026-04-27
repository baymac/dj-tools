#!/usr/bin/env python3
"""Analyse a single Beatport track for key, BPM, and energy.

Read-only — does not write to any database. Standalone helper, not part of the
DJ Studio → Rekordbox pipeline.

Usage:
    uv run local-analyse/beatport_analyze.py <beatport_url>

Example:
    uv run local-analyse/beatport_analyze.py https://www.beatport.com/track/title/18398870
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent
DJS_ANALYZE = SCRIPT_DIR / "djs_analyze.js"
DJ_LIBRARY  = Path.home() / "Music/DJ.Studio/Database/audio-library-table"

# Token locations: local store takes priority, falls back to chennai DB
_LOCAL_TOKEN_FILE = SCRIPT_DIR / ".beatport_token"
_CHENNAI_DB       = Path.home() / "conductor/workspaces/beatport/chennai/state/sync.db"

API_ROOT = "https://api.beatport.com/v4"

# DJ Studio numeric key (1-24, 1-indexed) → Camelot, matches CAMELOT_MAP in trackdb/schema.py
_CAMELOT_MAP: dict[int, str] = {
    1: "8B",  2: "3B",  3: "10B", 4: "5B",  5: "12B", 6: "7B",
    7: "2B",  8: "9B",  9: "4B",  10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A",
    19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A", 24: "10A",
}


def _camelot_for(key_num) -> str | None:
    if key_num is None:
        return None
    try:
        return _CAMELOT_MAP.get(int(key_num))
    except (TypeError, ValueError):
        return None


# ── Beatport API ───────────────────────────────────────────────────────────────

def _load_token() -> str | None:
    """Return a valid Bearer token. Local store first, chennai DB fallback."""
    import base64

    def _exp(token: str) -> float | None:
        try:
            payload = token.split()[-1].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except Exception:
            return None

    if _LOCAL_TOKEN_FILE.exists():
        try:
            data = json.loads(_LOCAL_TOKEN_FILE.read_text())
            token = data.get("token", "")
            exp = _exp(token)
            if token and (exp is None or exp > time.time() + 60):
                return token
        except Exception:
            pass

    if _CHENNAI_DB.exists():
        try:
            con = sqlite3.connect(str(_CHENNAI_DB))
            row = con.execute(
                "SELECT token FROM auth_cache WHERE service='beatport'"
            ).fetchone()
            con.close()
            if row:
                token = row[0]
                exp = _exp(token)
                if exp is None or exp > time.time() + 60:
                    return token
        except Exception:
            pass

    return None


def _api_get(path: str, token: str) -> dict:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    req = urllib.request.Request(
        url,
        headers={"authorization": token, "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_track(track_id: int, token: str) -> dict:
    return _api_get(f"/catalog/tracks/{track_id}/", token)


def parse_track_id(url: str) -> int:
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
    """MP3 → 44100 Hz mono float32-LE PCM via ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-i", str(mp3_path), "-ac", "1", "-ar", "44100",
         "-f", "f32le", "-y", str(pcm_path)],
        check=True, capture_output=True,
    )


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


def _estimate_key(samples, sr: int = 44100) -> str:
    """Krumhansl-Schmuckler key detection on a CQT chromagram."""
    import numpy as np
    import librosa

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


def run_djs_analysis(pcm_path: Path) -> dict:
    """Returns {key, camelotKey, mikKeyNr, bpm}.

    Key: Krumhansl-Schmuckler on CQT chromagram (Python/librosa).
    BPM: ai-beatgrid Node addon via djs_analyze.js.
    """
    import numpy as np

    raw = pcm_path.read_bytes()
    samples = np.frombuffer(raw, dtype="<f4")

    key_display = _estimate_key(samples, sr=44100)
    key_idx = KEY_MAP.get(key_display, -1)
    camelot = CAMELOT[key_idx] if key_idx >= 0 else None
    mik_key_nr = key_idx + 1 if key_idx >= 0 else None
    bpm = _run_djs_bpm(pcm_path)

    return {"key": key_display, "camelotKey": camelot, "mikKeyNr": mik_key_nr, "bpm": bpm}


def compute_energy(pcm_path: Path) -> int:
    """1-10 energy score from spectral brightness + onset density.

    Beatport previews are mastered to similar loudness, so raw RMS has no
    variance. Combines:
      - High-frequency energy ratio above 2 kHz: brighter tracks score higher
      - Onset strength mean: more hits per second → more percussive energy

    Calibrated against the observed mikEnergy distribution in DJ Studio
    (clusters at 5-7, rarely above 8).
    """
    import numpy as np
    import librosa

    raw = pcm_path.read_bytes()
    if not raw:
        return 5
    samples = np.frombuffer(raw, dtype="<f4")

    S = np.abs(librosa.stft(samples))
    freqs = librosa.fft_frequencies(sr=44100)
    hf_ratio = float(S[freqs > 2000, :].mean() / (S.mean() + 1e-9))

    onset_mean = float(librosa.onset.onset_strength(y=samples, sr=44100).mean())

    # hf_ratio: ~0.20 (dark/bass-heavy) to ~0.50 (bright/noisy)
    # onset_mean: ~0.5 (sparse/ambient) to ~5.0 (dense percussion)
    hf_norm  = max(0.0, min(1.0, (hf_ratio   - 0.20) / (0.50 - 0.20)))
    ons_norm = max(0.0, min(1.0, (onset_mean - 0.50) / (5.00 - 0.50)))

    combined = 0.5 * hf_norm + 0.5 * ons_norm
    score = 4 + combined * 5
    return max(1, min(10, round(score)))


# ── DJ Studio library lookup ───────────────────────────────────────────────────

def lookup_dj_studio(track_id: int) -> dict | None:
    """Return DJ Studio's audio-library-table entry for this Beatport track, if present."""
    library_key = f"beatport-sdk_{track_id}"
    if not DJ_LIBRARY.is_dir():
        return None
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


# ── Main ───────────────────────────────────────────────────────────────────────

def analyse_url(url: str, token: str) -> None:
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

    djs = lookup_dj_studio(track_id)
    if djs:
        mik_nr = djs.get("mikKey") or djs.get("camelotKey")
        existing_key = _camelot_for(mik_nr) or "?"
        print(f"  Already in DJ Studio: key={existing_key}  energy={djs.get('mikEnergy','?')}")

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse a single Beatport track for key, BPM, and energy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  uv run local-analyse/beatport_analyze.py https://www.beatport.com/track/title/18398870
        """,
    )
    parser.add_argument("url", help="Beatport track URL (single)")
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
        print("Error: No valid Beatport token found.", file=sys.stderr)
        print("Run: uv run local-analyse/beatport_auth.py login", file=sys.stderr)
        sys.exit(1)

    try:
        analyse_url(args.url, token)
    except Exception as e:
        print(f"  Error analysing {args.url}: {e}", file=sys.stderr)
        sys.exit(1)
    print()


if __name__ == "__main__":
    main()
