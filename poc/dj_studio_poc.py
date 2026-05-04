"""PoC: drive DJ Studio's full MIK pipeline on a single audio file.

Steps:
1. Decrypt DJ Studio's local refresh token (AES-256-CBC, hardcoded key)
2. Exchange for a short-lived access JWT (app-services.dj.studio)
3. Spawn the Node helper, which:
   - decodes the audio to 44.1k mono Float32
   - feeds it to DJ Studio's bundled MIK WASM extractor
   - POSTs the extracted features to cf.dj.studio/mixedinkey/analyze
4. Print response + compare to DJ Studio's already-stored value (if known)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Hardcoded AES key from /Applications/DJ.Studio.app/Contents/Resources/app.asar
# (extracted from main.js — DJ Studio uses this to encrypt its refresh token).
_DJS_AES_KEY = bytes.fromhex(
    "0e3eda35346762a8aa0d369c067f478747a9fce80d1f28fa3879a87236615047"
)
_TOKEN_FILE = Path.home() / "Library/Application Support/DJ.Studio/encryptedToken-v2.dat"
_REFRESH_URL = "https://app-services.dj.studio/api/login/v2/token/refresh/json"

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_HELPER = REPO_ROOT / "poc" / "mik_analyze.js"

# DJ Studio Camelot integer (0-23) -> Camelot string. From earlier reverse-eng.
MIK_CAMELOT = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
    12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
    18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
}


def decrypt_refresh_token() -> str:
    blob = json.loads(_TOKEN_FILE.read_text())
    iv = bytes.fromhex(blob["iv"])
    ct = bytes.fromhex(blob["token"])
    raw = Cipher(algorithms.AES(_DJS_AES_KEY), modes.CBC(iv)).decryptor()
    plain_padded = raw.update(ct) + raw.finalize()
    pad_len = plain_padded[-1]
    return plain_padded[:-pad_len].decode("utf-8")


def get_access_token(refresh_token: str) -> str:
    r = httpx.post(
        _REFRESH_URL,
        json={"refreshToken": refresh_token},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["token"]


def find_stored_metadata(audio_path: Path) -> dict | None:
    """For files cached by DJ Studio, find the matching audio-library-table entry
    via fileHash (the file in audioData is named file_<sha256>)."""
    name = audio_path.name
    if not name.startswith("file_"):
        return None
    file_hash = name.removeprefix("file_")
    lib = Path.home() / "Music/DJ.Studio/Database/audio-library-table"
    for shard in lib.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.iterdir():
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if d.get("fileHash") == file_hash:
                return d
    return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio", help="Path to a WAV/MP3 file (or a 'file_<hash>' from DJ Studio's audioData)")
    args = p.parse_args()

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.is_file():
        sys.exit(f"Not a file: {audio_path}")

    print(f"[1/4] Decrypting DJ Studio refresh token from {_TOKEN_FILE}")
    refresh_tok = decrypt_refresh_token()
    print(f"      refresh_token: {refresh_tok[:20]}…{refresh_tok[-10:]} ({len(refresh_tok)} chars)")

    print(f"[2/4] Exchanging refresh token for access JWT")
    access_tok = get_access_token(refresh_tok)
    print(f"      access_token: {access_tok[:30]}… ({len(access_tok)} chars)")

    print(f"[3/4] Spawning Node helper on {audio_path}")
    proc = subprocess.run(
        ["node", str(NODE_HELPER), str(audio_path), access_tok],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        print("--- node stderr ---")
        print(proc.stderr)
        sys.exit(f"Node helper failed (exit {proc.returncode})")

    result = json.loads(proc.stdout)
    server = result.get("server", {})
    body = server.get("body", {})

    print(f"[4/4] DONE  ({result['timing_ms']['total']/1000:.1f}s total: "
          f"decode={result['timing_ms']['decode']/1000:.1f}s, "
          f"wasm={result['timing_ms']['wasm']/1000:.1f}s, "
          f"server={result['timing_ms']['server']/1000:.1f}s)")
    print()
    print(f"  audio: {result['audio']['samples']} samples, "
          f"{result['audio']['duration_sec']:.2f}s")
    print()
    print(f"  WASM-local extracts:")
    print(f"    tempo (BPM):           {result['wasm']['tempo']:.3f}")
    print(f"    downbeat_time (s):     {result['wasm']['downbeat_time']:.3f}")
    print(f"    cue_point_start_beat:  {result['wasm']['cue_point_start_beat']}")
    print(f"    beat_grid length:      {result['wasm']['beat_grid_length']}")
    print(f"    energy segments:       {result['wasm']['energy_segment_count']}")
    print()
    print(f"  cf.dj.studio response (HTTP {server.get('httpStatus')}):")
    if server.get("httpStatus") != 200:
        print(f"    body: {body}")
    else:
        ks = body.get("KeySummary") or {}
        main_key = ks.get("MainKey")
        try:
            main_key_int = int(main_key)
            camelot = MIK_CAMELOT.get(main_key_int, "?")
        except (TypeError, ValueError):
            camelot = "?"
        print(f"    IsLicenseValid:         {body.get('IsLicenseValid')}")
        print(f"    KeySummary.MainKey:     {main_key}  → Camelot {camelot}")
        print(f"    KeySummary.SecondKey:   {ks.get('SecondKey')}")
        print(f"    KeySummary.confidence:  {ks.get('MainKeyConfidence'):.3f}" if ks.get('MainKeyConfidence') is not None else "    KeySummary.confidence: -")
        print(f"    OverallEnergy (1-10):   {body.get('OverallEnergy')}")
        ec = body.get("EnergyLevelSegments") or []
        cp = body.get("CuePoints") or []
        print(f"    EnergyLevelSegments:    {len(ec)} segments")
        print(f"    CuePoints:              {len(cp)} points")

    stored = find_stored_metadata(audio_path)
    if stored:
        print()
        print(f"  Stored DJ Studio metadata for this file:")
        print(f"    artist/title:    {stored['tag'].get('artist')} — {stored['tag'].get('title')}")
        print(f"    bpm:             {stored.get('bpm')}")
        print(f"    mikKey:          {stored.get('mikKey')}  → Camelot {MIK_CAMELOT.get(stored.get('mikKey'), '?')}")
        print(f"    mikEnergy:       {stored.get('mikEnergy')}")
        print(f"    duration:        {stored.get('duration')}")


if __name__ == "__main__":
    main()
