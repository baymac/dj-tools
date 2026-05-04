"""Path A pipeline: drive DJ Studio's bundled SDK + analysis libraries headlessly,
fetch full Beatport tracks via the user's authenticated SDK session, and write
real DJ Studio library entries (audio-library-table + track-structures-table +
audio-library-compressedAudioView*) so DJ Studio's own enrich-studio reads it back.

Requires DJ Studio to be QUIT before running — the SDK needs port 61894 + the
`.beatport/` cache files free of locks.

Flow:
  1. Verify DJ Studio process not running.
  2. Decrypt local refresh token from encryptedToken-v2.dat (AES-256-CBC).
  3. Exchange for short-lived access JWT via app-services.dj.studio.
  4. Spawn detect/dj_studio_sdk.js (long-running). Send init.
  5. For each track in the test table missing mik_key:
       a. Send {cmd: analyze, beatport_id}
       b. Read JSON response with full analysis (mikKey, mikEnergy, beats,
          phrases, stems compressed views, EnergyLevelSegments, CuePoints).
       c. Write to DJ Studio's library tables on disk.
  6. Send {cmd: exit}, close subprocess.

Caller then runs: `dj detect enrich-studio --test`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import psutil
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

from detect import db as detect_db

console = Console()

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_HELPER = REPO_ROOT / "detect" / "dj_studio_sdk.js"

DJ_STUDIO_DB = Path.home() / "Music" / "DJ.Studio" / "Database"
DJ_STUDIO_LIBRARY    = DJ_STUDIO_DB / "audio-library-table"
DJ_STUDIO_STRUCTURES = DJ_STUDIO_DB / "track-structures-table"
DJ_STUDIO_VOCALS = DJ_STUDIO_DB / "audio-library-compressedAudioViewVocals"
DJ_STUDIO_DRUMS  = DJ_STUDIO_DB / "audio-library-compressedAudioViewDrums"
DJ_STUDIO_BASS   = DJ_STUDIO_DB / "audio-library-compressedAudioViewBass"
DJ_STUDIO_MELODY = DJ_STUDIO_DB / "audio-library-compressedAudioViewMelody"

# DJ Studio token decrypt
_DJS_ENC_KEY = bytes.fromhex(
    "0e3eda35346762a8aa0d369c067f478747a9fce80d1f28fa3879a87236615047"
)
_DJS_TOKEN_FILE = Path.home() / "Library/Application Support/DJ.Studio/encryptedToken-v2.dat"
_DJS_REFRESH_URL = "https://app-services.dj.studio/api/login/v2/token/refresh/json"

# Camelot maps (DJ Studio uses 0-23, server returns Camelot strings)
MIK_CAMELOT_INT_TO_STR: dict[int, str] = {
    0: "8B",  1: "3B",  2: "10B", 3: "5B",  4: "12B", 5: "7B",
    6: "2B",  7: "9B",  8: "4B",  9: "11B", 10: "6B", 11: "1B",
    12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
    18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
}
MIK_CAMELOT_STR_TO_INT = {v: k for k, v in MIK_CAMELOT_INT_TO_STR.items()}

KIND = "beatport-sdk"


# ── 1. DJ Studio process guard ────────────────────────────────────────────────

def is_dj_studio_running() -> bool:
    """True if DJ.Studio.app is running (would conflict with our SDK use)."""
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "dj.studio" in name or "dj studio" in name:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# ── 2. DJ Studio access JWT ───────────────────────────────────────────────────

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


# ── 3. Library entry writers ──────────────────────────────────────────────────

def _shard(library_key: str) -> str:
    return hashlib.sha1(library_key.encode()).hexdigest()[:2]


def _existing_entry_path(library_key: str, root: Path) -> Optional[Path]:
    if not root.is_dir():
        return None
    for shard in root.iterdir():
        if shard.is_dir():
            cand = shard / library_key
            if cand.is_file():
                return cand
    return None


def _camelot_to_int(camelot: object) -> Optional[int]:
    if camelot is None:
        return None
    if isinstance(camelot, int):
        return camelot if 0 <= camelot <= 23 else None
    s = str(camelot).strip().upper()
    if s.isdigit():
        n = int(s)
        return n if 0 <= n <= 23 else None
    return MIK_CAMELOT_STR_TO_INT.get(s)


def _shape_result(beatport_id: int, result: dict) -> Optional[dict]:
    """Pull DJ-Studio-shaped fields out of the Node helper output."""
    server = result.get("server") or {}
    if not server.get("ok"):
        return None
    body = server.get("body") or {}
    if not body.get("IsLicenseValid"):
        return None

    main_key = (body.get("KeySummary") or {}).get("MainKey")
    mik_key_int = _camelot_to_int(main_key)
    if mik_key_int is None:
        return None

    try:
        mik_nrg_int = int(round(float(body.get("OverallEnergy"))))
    except (TypeError, ValueError):
        return None
    if not 1 <= mik_nrg_int <= 10:
        return None

    duration_sec = float(result.get("duration_sec") or 0)
    bpm_beatgrid = result.get("beatgrid", {}).get("bpm")
    if not bpm_beatgrid and (beats := result.get("beatgrid", {}).get("beats")):
        if len(beats) >= 2:
            intervals = [b["time"] - a["time"] for a, b in zip(beats[:-1], beats[1:])]
            mean_int = sum(intervals) / len(intervals)
            bpm_beatgrid = 60 / mean_int if mean_int else 0
    bpm = float(bpm_beatgrid or result.get("wasm", {}).get("tempo") or 0)

    cue_points = []
    for cp in (body.get("CuePoints") or []):
        cue_points.append({
            "beat": cp.get("Beat", 0),
            "time": cp.get("Time", 0),
            "length": 0,
            "type": cp.get("Type", 0),
            "name": "",
        })

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

    beats = result.get("beatgrid", {}).get("beats", [])
    beat_data: list[dict] = []
    for nr, b in enumerate(beats):
        beat_data.append({
            "nr": nr,
            "time": b.get("time", 0),
            "originalTime": b.get("time", 0),
            "type": 0 if b.get("position", 1) != 1 else -1,  # downbeat marker
            "phraseNr": 0,
            "energyLevelNr": 0,
        })

    bpm_line = []
    if bpm and duration_sec:
        bpm_line = [
            {"time": 0, "beatNr": 0, "value": bpm, "flags": 0},
            {"time": duration_sec, "beatNr": len(beats), "value": bpm, "flags": 0},
        ]

    beat_grids = [{
        "beatDataSource": "ai3",
        "madeOn": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "transientScore": 0,
        "bpm": int(round(bpm)) if bpm else 0,
        "minBpm": int(round(bpm)) if bpm else 0,
        "maxBpm": int(round(bpm)) if bpm else 0,
    }]

    return {
        "bpm": bpm,
        "duration_sec": duration_sec,
        "mik_key_int": mik_key_int,
        "mik_nrg_int": mik_nrg_int,
        "cue_points": cue_points,
        "energy_level_segments": energy_level_segments,
        "beat_data": beat_data,
        "bpm_line": bpm_line,
        "beat_grids": beat_grids,
        "stems_compressed_b64": result.get("stems_compressed_b64") or {},
    }


def _build_library_entry(
    *, beatport_id: int, artist: str, title: str, shaped: dict
) -> dict:
    library_key = f"{KIND}_{beatport_id}"
    camelot_str = MIK_CAMELOT_INT_TO_STR.get(shaped["mik_key_int"], "")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "key": library_key, "name": title or "", "kind": KIND,
        "size": 0, "fileHash": "", "type": "",
        "lastModified": now_iso, "importDate": now_iso,
        "rating": 0, "method": 0, "inLibrary": True, "isTemporary": False,
        "tag": {
            "genre": "", "artist": artist or "", "album": "", "track": "",
            "title": title or "", "year": "",
            "bpm": str(int(round(shaped["bpm"]))) if shaped["bpm"] else "",
            "key": camelot_str, "comment": "", "premium": False,
            "releaseYear": "", "label": "",
        },
        "mikKey": shaped["mik_key_int"], "mikEnergy": shaped["mik_nrg_int"],
        "energy": 0, "danceability": 0, "mood": 0,
        "duration": float(shaped["duration_sec"]), "sampleRate": 44100,
        "pictureType": "",
        "image": {"type": "image/jpeg"},
        "image64": {"type": "image/jpeg"},
        "image512": {"type": "image/jpeg"},
        "imageUrl": "",
        "bpm": float(shaped["bpm"]) if shaped["bpm"] else 0,
        "camelotKey": shaped["mik_key_int"],
        "originalCamelotKey": shaped["mik_key_int"],
        "noteKey": shaped["mik_key_int"],
        "autoGainCalculated": False, "autoGain": 1,
        "cueData": {
            "loopMode": 0,
            "systemCuePoints": shaped["cue_points"],
            "hotCuePoints": [], "memCuePoints": [],
        },
        "externalRec": {"UUID": str(beatport_id), "fileLocationPath": ""},
        "analyzeVersion": "",
        "mixedInKeyAnalyzeVersion": "1",
        "rekordboxAnalyzeVersion": "",
        "seratoAnalyzeVersion": "",
        "structureKey": library_key,
        "audioCleaned": False, "cleanedVersion": "",
        "beatQuantize": True,
        "bpmLine": shaped["bpm_line"],
        "beatGrids": shaped["beat_grids"],
        "beatDataSource": "ai3",
        "originalAudiofileRecordKey": "",
        "bpmMultiplier": 1, "stemsType": "", "sourceKind": "",
    }


def _write_library_entry(library_key: str, entry: dict) -> Path:
    out_path = _existing_entry_path(library_key, DJ_STUDIO_LIBRARY)
    if out_path is None:
        shard_dir = DJ_STUDIO_LIBRARY / _shard(library_key)
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_path = shard_dir / library_key
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            for k in ("mikKey", "mikEnergy", "camelotKey", "originalCamelotKey",
                      "noteKey", "mixedInKeyAnalyzeVersion", "bpm", "duration",
                      "bpmLine", "beatGrids", "beatDataSource"):
                existing[k] = entry[k]
            existing.setdefault("tag", {})["key"] = entry["tag"]["key"]
            existing["tag"]["bpm"] = entry["tag"]["bpm"]
            if entry["cueData"]["systemCuePoints"]:
                existing.setdefault("cueData", {})["systemCuePoints"] = entry["cueData"]["systemCuePoints"]
            out_path.write_text(json.dumps(existing, separators=(",", ":")))
            return out_path
        except Exception:
            pass
    out_path.write_text(json.dumps(entry, separators=(",", ":")))
    return out_path


def _write_track_structures(library_key: str, shaped: dict) -> None:
    if not shaped["beat_data"] and not shaped["energy_level_segments"]:
        return
    entry = {
        "key": library_key,
        "beatData": shaped["beat_data"],
        "bpmLine": shaped["bpm_line"],
        "phraseData": [],
        "energyLevelData": shaped["energy_level_segments"],
    }
    out_path = _existing_entry_path(library_key, DJ_STUDIO_STRUCTURES)
    if out_path is None:
        shard_dir = DJ_STUDIO_STRUCTURES / _shard(library_key)
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_path = shard_dir / library_key
    out_path.write_text(json.dumps(entry, separators=(",", ":")))


def _write_compressed_view(root: Path, library_key: str, b64: Optional[str]) -> None:
    if not b64:
        return
    out_path = _existing_entry_path(library_key, root)
    if out_path is None:
        shard_dir = root / _shard(library_key)
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_path = shard_dir / library_key
    out_path.write_bytes(base64.b64decode(b64))


# ── 4. Long-running Node helper ──────────────────────────────────────────────

class SdkHelper:
    """Wraps the dj_studio_sdk.js subprocess with line-based JSON IPC."""

    def __init__(self, djs_access_jwt: str, *, staging: bool = False, verbose: bool = False):
        self._jwt = djs_access_jwt
        self._staging = staging
        self._verbose = verbose
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["node", str(NODE_HELPER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._send({"cmd": "init", "stagingApi": self._staging, "djsAccessJwt": self._jwt})
        # Drain log lines until we see "ready"
        while True:
            evt = self._read()
            if evt is None:
                stderr = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(f"helper exited before ready: {stderr.strip()[:500]}")
            if evt.get("event") == "ready":
                break
            if evt.get("event") == "log" and self._verbose:
                console.log(f"[dim]helper:[/dim] {evt.get('message')}")
            if evt.get("event") == "error":
                raise RuntimeError(f"helper init error: {evt.get('message')}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._send({"cmd": "exit"})
            self._proc.wait(timeout=10)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> Optional[dict]:
        assert self._proc and self._proc.stdout
        line = self._proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:
            return {"event": "log", "message": line.rstrip()}

    def analyze(self, beatport_id: int) -> dict:
        """Send analyze + read events until we get the matching analysis or error.

        Returns {"ok": True, "result": {...}} or {"ok": False, "message": "..."}.
        """
        self._send({"cmd": "analyze", "beatport_id": beatport_id})
        while True:
            evt = self._read()
            if evt is None:
                stderr = self._proc.stderr.read() if self._proc.stderr else ""
                return {"ok": False, "message": f"helper closed: {stderr.strip()[:300]}"}
            kind = evt.get("event")
            if kind == "log" and self._verbose:
                console.log(f"[dim]helper:[/dim] {evt.get('message')}")
            elif kind == "analysis" and evt.get("beatport_id") == beatport_id:
                return {"ok": True, "result": evt["result"]}
            elif kind == "error" and evt.get("beatport_id") == beatport_id:
                return {"ok": False, "message": evt.get("message", "unknown")}
            elif kind == "error":
                # Helper-level error (not track-specific) — still surface it
                console.log(f"[red]helper error:[/red] {evt.get('message')}")
                return {"ok": False, "message": evt.get("message", "unknown")}


# ── 5. Top-level runner ───────────────────────────────────────────────────────

def run_import_to_studio(
    *,
    table: str = "enriched_tracks_test",
    limit: int = 0,
    keep_temp: bool = False,  # unused now (no temp dir), kept for CLI compat
    verbose: bool = False,
) -> None:
    if is_dj_studio_running():
        console.print(
            "[red]DJ Studio is currently running.[/red]\n"
            "Quit DJ.Studio (Cmd+Q) before running this command — its SDK conflicts "
            "with our pipeline (port 61894 + cache file locks)."
        )
        return

    console.print(f"[bold]import-to-studio[/bold] ← [cyan]{table}[/cyan]  (Path A: full tracks via DJ Studio SDK)")

    rows = detect_db.get_studio_enrichable_tracks(table=table)
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("Nothing to import — every row already has mik_key.")
        return
    console.print(f"{len(rows)} tracks queued.")

    try:
        access_jwt = _get_dj_studio_access_token()
    except Exception as e:
        console.print(f"[red]Failed to get DJ Studio access token: {e}[/red]")
        console.print(
            "[yellow]Open DJ Studio briefly to refresh its session, then quit and re-run.[/yellow]"
        )
        return

    counts = {"seen": 0, "ok": 0, "fail": 0}

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=console,
    )

    with SdkHelper(access_jwt, verbose=verbose) as helper, progress:
        task = progress.add_task("Analyzing…", total=len(rows))

        for row in rows:
            counts["seen"] += 1
            bid = row["beatport_id"]
            artist = row["artist"] or ""
            title = row["title"] or ""
            progress.update(task, advance=1, description=f"{artist} — {title}")

            res = helper.analyze(bid)
            if not res["ok"]:
                counts["fail"] += 1
                if verbose:
                    progress.log(f"[red]bp:{bid}[/red] {res['message'][:200]}")
                continue

            shaped = _shape_result(bid, res["result"])
            if shaped is None:
                counts["fail"] += 1
                if verbose:
                    progress.log(f"[yellow]bp:{bid} unusable analyzer output[/yellow]")
                continue

            library_key = f"{KIND}_{bid}"
            entry = _build_library_entry(beatport_id=bid, artist=artist, title=title, shaped=shaped)
            _write_library_entry(library_key, entry)
            _write_track_structures(library_key, shaped)

            stems_b64 = shaped["stems_compressed_b64"] or {}
            _write_compressed_view(DJ_STUDIO_VOCALS, library_key, stems_b64.get("vocals"))
            _write_compressed_view(DJ_STUDIO_DRUMS,  library_key, stems_b64.get("drums"))
            _write_compressed_view(DJ_STUDIO_BASS,   library_key, stems_b64.get("bass"))
            # DJ Studio's "melody" stem is what Demucs calls "other".
            _write_compressed_view(DJ_STUDIO_MELODY, library_key, stems_b64.get("other"))

            counts["ok"] += 1
            if verbose:
                t = res["result"].get("timing_ms", {})
                progress.log(
                    f"[green]bp:{bid}[/green] "
                    f"key={MIK_CAMELOT_INT_TO_STR.get(shaped['mik_key_int'])}  "
                    f"nrg={shaped['mik_nrg_int']}  "
                    f"bpm={shaped['bpm']:.1f}  "
                    f"({t.get('total', 0)/1000:.1f}s: fetch={t.get('fetch',0)/1000:.1f}, "
                    f"mik={t.get('mik',0)/1000:.1f}, srv={t.get('server',0)/1000:.1f}, "
                    f"bg={t.get('beatgrid',0)/1000:.1f}, st={t.get('stems',0)/1000:.1f})"
                )

    console.print()
    console.print(f"[bold]Done.[/bold] {counts['ok']}/{counts['seen']} written  ({counts['fail']} failed)")
    if counts["ok"]:
        flag = "  --test" if "test" in table else ""
        console.print(f"[dim]Next:[/dim] [cyan]uv run dj_cli.py detect enrich-studio{flag}[/cyan]")
