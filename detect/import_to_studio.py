"""Pipeline: enriched_tracks → Beatport preview MP3 → DJ Studio analysis → DJ Studio library JSON.

Runs DJ Studio's full analysis chain *without opening DJ Studio's UI*:
  1. Decrypt DJ Studio's local refresh token (AES-256-CBC, hardcoded key from main.js).
  2. Exchange for a short-lived access JWT via app-services.dj.studio.
  3. For each enriched_tracks_test row with mik_key IS NULL:
     a. Fetch Beatport preview URL (sample_url field).
     b. Download 30s preview MP3 to a temp dir.
     c. Spawn detect/dj_studio_analyze.js — decodes audio, runs the bundled MIK
        WASM extractor + ai-beatgrid native addon, POSTs features to
        cf.dj.studio/mixedinkey/analyze for the integer key + 1-10 energy +
        EnergyLevelSegments + CuePoints.
     d. Write a real DJ Studio audio-library-table entry (and matching
        track-structures-table entry) so DJ Studio's own enrich-studio
        pipeline can read it back.
  4. Caller runs `dj detect enrich-studio --test` to copy mik_key/mik_nrg into
     enriched_tracks_test.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from connections import beatport as bp_api
from detect import db as detect_db
from detect.enrich import _get_token

console = Console()

# ── DJ Studio paths + constants ───────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_HELPER = REPO_ROOT / "detect" / "dj_studio_analyze.js"

DJ_STUDIO_LIBRARY = Path.home() / "Music" / "DJ.Studio" / "Database" / "audio-library-table"
DJ_STUDIO_STRUCTURES = Path.home() / "Music" / "DJ.Studio" / "Database" / "track-structures-table"

_DJS_ENC_KEY = bytes.fromhex(
    "0e3eda35346762a8aa0d369c067f478747a9fce80d1f28fa3879a87236615047"
)
_DJS_TOKEN_FILE = Path.home() / "Library/Application Support/DJ.Studio/encryptedToken-v2.dat"
_DJS_REFRESH_URL = "https://app-services.dj.studio/api/login/v2/token/refresh/json"

# Verified against real DJ.Studio audio-library-table entries.
MIK_CAMELOT_INT_TO_STR: dict[int, str] = {
    0: "8B",  1: "3B",  2: "10B", 3: "5B",  4: "12B", 5: "7B",
    6: "2B",  7: "9B",  8: "4B",  9: "11B", 10: "6B", 11: "1B",
    12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
    18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
}
MIK_CAMELOT_STR_TO_INT = {v: k for k, v in MIK_CAMELOT_INT_TO_STR.items()}

KIND = "beatport-sdk"


# ── DJ Studio auth ────────────────────────────────────────────────────────────

def _decrypt_dj_studio_refresh_token() -> str:
    blob = json.loads(_DJS_TOKEN_FILE.read_text())
    iv = bytes.fromhex(blob["iv"])
    ct = bytes.fromhex(blob["token"])
    raw = Cipher(algorithms.AES(_DJS_ENC_KEY), modes.CBC(iv)).decryptor()
    plain_padded = raw.update(ct) + raw.finalize()
    pad_len = plain_padded[-1]
    return plain_padded[:-pad_len].decode("utf-8")


def _get_dj_studio_access_token() -> str:
    refresh = _decrypt_dj_studio_refresh_token()
    r = httpx.post(
        _DJS_REFRESH_URL,
        json={"refreshToken": refresh},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["token"]


# ── Beatport preview download ─────────────────────────────────────────────────

def _fetch_preview_url(beatport: bp_api.Beatport, track_id: int) -> Optional[str]:
    return beatport.preview_url(track_id)


def _download_preview(url: str, dest: Path, *, beatport_id: int) -> bool:
    try:
        with httpx.stream("GET", url, timeout=30, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes(65536):
                    fh.write(chunk)
        return True
    except Exception as e:
        console.log(f"[yellow]download failed bp:{beatport_id}: {e}[/yellow]")
        return False


# ── DJ Studio analyzer (Node helper) ──────────────────────────────────────────

def _analyze_with_dj_studio(audio_path: Path, access_token: str, *, timeout_s: int = 120) -> Optional[dict]:
    """Spawn detect/dj_studio_analyze.js. Returns the parsed JSON or None on error."""
    try:
        proc = subprocess.run(
            ["node", str(NODE_HELPER), str(audio_path), access_token],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        console.log(f"[yellow]analyzer timeout for {audio_path.name}[/yellow]")
        return None
    if proc.returncode != 0:
        console.log(f"[red]analyzer failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}[/red]")
        return None
    try:
        return json.loads(proc.stdout)
    except Exception as e:
        console.log(f"[red]analyzer JSON parse failed: {e}[/red]")
        return None


# ── DJ Studio library entry writers ───────────────────────────────────────────

def _shard(library_key: str) -> str:
    """Any 2-hex shard name works — DJ Studio scans all shards on load."""
    return hashlib.sha1(library_key.encode()).hexdigest()[:2]


def _existing_entry_path(library_key: str, root: Path) -> Optional[Path]:
    """Find an existing entry in any shard (DJ Studio puts them in arbitrary shards)."""
    if not root.is_dir():
        return None
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        candidate = shard / library_key
        if candidate.is_file():
            return candidate
    return None


def _camelot_to_int(camelot: str | int | None) -> Optional[int]:
    if camelot is None:
        return None
    if isinstance(camelot, int):
        return camelot if 0 <= camelot <= 23 else None
    s = str(camelot).strip().upper()
    if s.isdigit():
        n = int(s)
        return n if 0 <= n <= 23 else None
    return MIK_CAMELOT_STR_TO_INT.get(s)


def _build_library_entry(
    *,
    beatport_id: int,
    artist: str,
    title: str,
    duration_sec: float,
    bpm: float,
    mik_key_int: int,
    mik_nrg_int: int,
    cue_points: list,
    bpm_line: list,
    beat_grids: list,
) -> dict:
    library_key = f"{KIND}_{beatport_id}"
    camelot_str = MIK_CAMELOT_INT_TO_STR.get(mik_key_int, "")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return {
        "key":   library_key,
        "name":  title or "",
        "kind":  KIND,
        "size":  0,
        "fileHash": "",
        "type":  "",
        "lastModified": now_iso,
        "importDate":   now_iso,
        "rating": 0, "method": 0,
        "inLibrary": True, "isTemporary": False,
        "tag": {
            "genre": "", "artist": artist or "", "album": "",
            "track": "", "title": title or "", "year": "",
            "bpm": str(int(round(bpm))) if bpm else "",
            "key": camelot_str, "comment": "",
            "premium": False, "releaseYear": "", "label": "",
        },
        "mikKey":   mik_key_int,
        "mikEnergy": mik_nrg_int,
        "energy": 0, "danceability": 0, "mood": 0,
        "duration": float(duration_sec),
        "sampleRate": 44100,
        "pictureType": "",
        "image": {"type": "image/jpeg"},
        "image64": {"type": "image/jpeg"},
        "image512": {"type": "image/jpeg"},
        "imageUrl": "",
        "bpm": float(bpm) if bpm else 0,
        "camelotKey":         mik_key_int,
        "originalCamelotKey": mik_key_int,
        "noteKey":            mik_key_int,
        "autoGainCalculated": False,
        "autoGain": 1,
        "cueData": {
            "loopMode": 0,
            "systemCuePoints": cue_points or [],
            "hotCuePoints": [],
            "memCuePoints": [],
        },
        "externalRec": {"UUID": str(beatport_id), "fileLocationPath": ""},
        "analyzeVersion": "",
        "mixedInKeyAnalyzeVersion": "1",
        "rekordboxAnalyzeVersion": "",
        "seratoAnalyzeVersion": "",
        "structureKey": library_key,
        "audioCleaned": False, "cleanedVersion": "",
        "beatQuantize": True,
        "bpmLine": bpm_line or [],
        "beatGrids": beat_grids or [],
        "beatDataSource": "fixed",
        "originalAudiofileRecordKey": "",
        "bpmMultiplier": 1, "stemsType": "", "sourceKind": "",
    }


def _write_library_entry(library_key: str, entry: dict) -> Path:
    """Write/merge audio-library-table entry. Preserves existing fields if present."""
    out_path = _existing_entry_path(library_key, DJ_STUDIO_LIBRARY)
    if out_path is None:
        shard_dir = DJ_STUDIO_LIBRARY / _shard(library_key)
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_path = shard_dir / library_key
        out_path.write_text(json.dumps(entry, separators=(",", ":")))
        return out_path

    # Merge: replace MIK + analysis fields, keep everything else.
    try:
        existing = json.loads(out_path.read_text())
    except Exception:
        out_path.write_text(json.dumps(entry, separators=(",", ":")))
        return out_path

    for k in ("mikKey", "mikEnergy", "camelotKey", "originalCamelotKey", "noteKey",
              "mixedInKeyAnalyzeVersion", "bpm", "duration"):
        existing[k] = entry[k]
    existing.setdefault("tag", {})["key"] = entry["tag"]["key"]
    existing["tag"]["bpm"] = entry["tag"]["bpm"]
    if entry["cueData"]["systemCuePoints"]:
        existing.setdefault("cueData", {})["systemCuePoints"] = entry["cueData"]["systemCuePoints"]
    if entry["bpmLine"]:
        existing["bpmLine"] = entry["bpmLine"]
    if entry["beatGrids"]:
        existing["beatGrids"] = entry["beatGrids"]
    out_path.write_text(json.dumps(existing, separators=(",", ":")))
    return out_path


def _write_track_structures(
    library_key: str,
    *,
    beats: list[dict],
    bpm_line: list[dict],
    energy_level_segments: list,
) -> Optional[Path]:
    """Write track-structures-table entry: beatData + bpmLine + phraseData + energyLevelData.

    Format mirrors what DJ Studio writes (verified against an existing entry):
      {
        "key": "<library_key>",
        "beatData":  [{nr, time, originalTime, type, phraseNr, energyLevelNr}, ...],
        "bpmLine":   [],   # we leave empty for fixed-tempo
        "phraseData": [],  # not populated yet
        "energyLevelData": [...]
      }
    """
    if not beats and not energy_level_segments:
        return None

    entry = {
        "key": library_key,
        "beatData": beats,
        "bpmLine": bpm_line or [],
        "phraseData": [],
        "energyLevelData": energy_level_segments or [],
    }
    out_path = _existing_entry_path(library_key, DJ_STUDIO_STRUCTURES)
    if out_path is None:
        shard_dir = DJ_STUDIO_STRUCTURES / _shard(library_key)
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_path = shard_dir / library_key
    out_path.write_text(json.dumps(entry, separators=(",", ":")))
    return out_path


def _shape_analyzer_output(analysis: dict, beatport_id: int) -> Optional[dict]:
    """Translate the Node helper's JSON into DJ Studio library + structure fields.

    Returns dict with keys: bpm, duration_sec, mik_key_int, mik_nrg_int,
    cue_points, bpm_line, beat_grids, beats, energy_level_segments.
    None if the analyzer didn't produce a usable result.
    """
    server = analysis.get("server") or {}
    body = server.get("body") or {}
    if server.get("httpStatus") != 200 or not body.get("IsLicenseValid"):
        return None

    key_summary = body.get("KeySummary") or {}
    main_key = key_summary.get("MainKey")
    mik_key_int = _camelot_to_int(main_key)
    if mik_key_int is None:
        console.log(f"[yellow]bp:{beatport_id} unrecognised key '{main_key}'[/yellow]")
        return None

    overall_energy = body.get("OverallEnergy")
    try:
        mik_nrg_int = int(round(float(overall_energy)))
    except (TypeError, ValueError):
        return None
    if not 1 <= mik_nrg_int <= 10:
        return None

    audio = analysis.get("audio") or {}
    duration_sec = float(audio.get("duration_sec") or 0)

    # Prefer ai-beatgrid's BPM when present (more accurate than MIK WASM tempo).
    beatgrid = analysis.get("beatgrid") or {}
    bpm = float(beatgrid.get("bpm") or analysis.get("wasm", {}).get("tempo") or 0)

    # Build a fixed-tempo beatGrids stub so DJ Studio's UI can show one.
    beat_grids = [{
        "beatDataSource": "fixed",
        "madeOn": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "transientScore": 0,
        "bpm": int(round(bpm)) if bpm else 0,
        "minBpm": int(round(bpm)) if bpm else 0,
        "maxBpm": int(round(bpm)) if bpm else 0,
    }]

    # bpmLine for fixed tempo: two anchor points (start, end).
    bpm_line = []
    if bpm and duration_sec:
        bpm_line = [
            {"time": 0, "beatNr": 0, "value": bpm, "flags": 0},
            {"time": duration_sec, "beatNr": 0, "value": bpm, "flags": 0},
        ]

    # Cue points come from the cf.dj.studio response.
    cue_points = []
    for cp in (body.get("CuePoints") or []):
        # Server's CuePoint shape: {Time, Beat, Type, ...}.  DJ Studio's stored
        # systemCuePoints shape: {beat, time, length, type, name}.
        cue_points.append({
            "beat": cp.get("Beat", 0),
            "time": cp.get("Time", 0),
            "length": 0,
            "type": cp.get("Type", 0),
            "name": "",
        })

    # Energy level segments → DJ Studio's energyLevelData.
    energy_level_segments = []
    for i, seg in enumerate(body.get("EnergyLevelSegments") or []):
        energy_level_segments.append({
            "nr": i,
            "startBeatNr": seg.get("StartBeat", 0),
            "beatLength": seg.get("BeatLength", 0),
            "startTime": seg.get("StartTime", 0),
            "endTime": seg.get("EndTime", 0),
            "type": 100,
            "mood": 100,
            "mikEnergy": seg.get("EnergyLevel", 0),
            "mikVolume": seg.get("VolumeRmsDb", 0),
            "label": str(seg.get("EnergyLevel", "")),
            "comment": "from mixed in key",
        })

    # Beat data — from ai-beatgrid (truncated preview only in JSON; for the
    # structures entry we'd need the full list, but the Node helper currently
    # only returns the preview. Keep empty for now; full beat list can be added
    # by widening the Node output schema if needed.)
    beats: list[dict] = []

    return {
        "bpm": bpm,
        "duration_sec": duration_sec,
        "mik_key_int": mik_key_int,
        "mik_nrg_int": mik_nrg_int,
        "cue_points": cue_points,
        "bpm_line": bpm_line,
        "beat_grids": beat_grids,
        "beats": beats,
        "energy_level_segments": energy_level_segments,
    }


# ── Top-level runner ──────────────────────────────────────────────────────────

def run_import_to_studio(
    *,
    table: str = "enriched_tracks_test",
    limit: int = 0,
    keep_temp: bool = False,
    verbose: bool = False,
) -> None:
    console.print(f"[bold]import-to-studio[/bold] ← [cyan]{table}[/cyan]  (via DJ Studio analyzer)")

    rows = detect_db.get_studio_enrichable_tracks(table=table)
    if limit:
        rows = rows[:limit]

    if not rows:
        console.print("Nothing to import — every row already has mik_key.")
        return

    console.print(f"{len(rows)} tracks to process")

    # 1. Decrypt + refresh DJ Studio token (used for cf.dj.studio/mixedinkey/analyze).
    try:
        access_token = _get_dj_studio_access_token()
        console.print(f"[dim]DJ Studio access token: {access_token[:30]}…[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to get DJ Studio access token: {e}[/red]")
        console.print(
            "[yellow]Make sure DJ Studio is installed and you've signed in at least once "
            "so the encrypted token at ~/Library/Application Support/DJ.Studio/encryptedToken-v2.dat exists.[/yellow]"
        )
        return

    # 2. Beatport client (for sample_url lookup).
    bp_token = _get_token()
    bp_http = bp_api.make_client(bp_token)
    beatport = bp_api.Beatport(client=bp_http)

    tmp_root = Path(tempfile.mkdtemp(prefix="dj_studio_import_"))
    console.print(f"[dim]Temp dir: {tmp_root}[/dim]")

    counts = {"seen": 0, "no_preview": 0, "download_fail": 0, "analyze_fail": 0, "wrote": 0}

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=console,
    )

    try:
        with progress:
            t = progress.add_task("Analyzing…", total=len(rows))
            for row in rows:
                counts["seen"] += 1
                bid = row["beatport_id"]
                artist = row["artist"] or ""
                title = row["title"] or ""
                progress.update(t, advance=1, description=f"{artist} — {title}")

                preview_url = _fetch_preview_url(beatport, bid)
                if not preview_url:
                    counts["no_preview"] += 1
                    if verbose:
                        progress.log(f"[yellow]no preview:[/yellow] bp:{bid} {artist} — {title}")
                    continue

                mp3 = tmp_root / f"beatport_{bid}.mp3"
                if not _download_preview(preview_url, mp3, beatport_id=bid):
                    counts["download_fail"] += 1
                    continue

                analysis = _analyze_with_dj_studio(mp3, access_token)
                if analysis is None:
                    counts["analyze_fail"] += 1
                    continue

                shaped = _shape_analyzer_output(analysis, beatport_id=bid)
                if shaped is None:
                    counts["analyze_fail"] += 1
                    if verbose:
                        progress.log(f"[yellow]analysis unusable:[/yellow] bp:{bid}")
                    continue

                library_key = f"{KIND}_{bid}"
                lib_entry = _build_library_entry(
                    beatport_id=bid,
                    artist=artist,
                    title=title,
                    duration_sec=shaped["duration_sec"],
                    bpm=shaped["bpm"],
                    mik_key_int=shaped["mik_key_int"],
                    mik_nrg_int=shaped["mik_nrg_int"],
                    cue_points=shaped["cue_points"],
                    bpm_line=shaped["bpm_line"],
                    beat_grids=shaped["beat_grids"],
                )
                _write_library_entry(library_key, lib_entry)
                _write_track_structures(
                    library_key,
                    beats=shaped["beats"],
                    bpm_line=shaped["bpm_line"],
                    energy_level_segments=shaped["energy_level_segments"],
                )

                counts["wrote"] += 1
                if verbose:
                    progress.log(
                        f"[green]wrote:[/green] {library_key}  "
                        f"key={MIK_CAMELOT_INT_TO_STR.get(shaped['mik_key_int'])}  "
                        f"nrg={shaped['mik_nrg_int']}  "
                        f"bpm={shaped['bpm']:.1f}"
                    )

        console.print()
        console.print(f"[bold]Done.[/bold] {counts['wrote']}/{counts['seen']} written")
        if counts["no_preview"]:
            console.print(f"  [yellow]no preview URL: {counts['no_preview']}[/yellow]")
        if counts["download_fail"]:
            console.print(f"  [yellow]download failed: {counts['download_fail']}[/yellow]")
        if counts["analyze_fail"]:
            console.print(f"  [yellow]analysis failed: {counts['analyze_fail']}[/yellow]")
        if counts["wrote"]:
            test_flag = "  --test" if "test" in table else ""
            console.print(
                f"\n[dim]Next:[/dim] [cyan]uv run dj_cli.py detect enrich-studio{test_flag}[/cyan]"
            )

    finally:
        bp_http.close()
        if keep_temp:
            console.print(f"[dim]Kept temp dir: {tmp_root}[/dim]")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
