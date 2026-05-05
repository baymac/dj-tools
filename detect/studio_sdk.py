"""Shared utilities for driving DJ Studio's bundled SDK headlessly.

Used by `dj detect studio-analyse` to populate enriched_tracks_analysis. Does
not write anything to DJ Studio's own filesystem — the SDK is purely an
analysis engine here.

Requires DJ Studio to be QUIT — its SDK conflicts with our use (port 61894 +
.beatport/ cache locks). The Node helper is detect/dj_studio_sdk.js.

Flow per track:
  1. SdkHelper sends {cmd: analyze, beatport_id} to the long-running Node helper.
  2. Helper downloads the full track audio via DJ Studio's beatport-sdk, runs
     MIK WASM (key + overall energy), cf.dj.studio classifier (energy segments
     + cue points), ai-beatgrid (beats + downbeats), ai-stems / Demucs
     (4-way separation + per-bucket RMS).
  3. Result returns to Python; _shape_result decodes per-bucket stem RMS into
     1Hz curves + per-energy-segment averages, builds the analysis_json blob.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import psutil
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from rich.console import Console

from paths import STATE_DIR

console = Console()

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_HELPER = REPO_ROOT / "detect" / "dj_studio_sdk.js"

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

# Tracks shorter than this are reliably under ai-beatgrid's working window
# and Demucs needs a few seconds of audio to separate stems — skip them at
# queue time rather than burn ~30s/track only to commit empty data.
MIN_DURATION_MS = 30_000

# Persistent record of helper-level failures (helper.analyze returned ok=False
# OR _shape_result rejected the response). Tracks failing N consecutive times
# are auto-skipped on subsequent runs to avoid infinite recycle.
FAILURES_FILE = STATE_DIR / "studio_analyse_failures.json"
MAX_FAILURE_ATTEMPTS = 3


# ── Failure sidecar ───────────────────────────────────────────────────────────

def _load_failures() -> dict[int, dict]:
    if not FAILURES_FILE.exists():
        return {}
    try:
        raw = json.loads(FAILURES_FILE.read_text())
    except Exception:
        return {}
    return {int(k): v for k, v in raw.items() if str(k).isdigit()}


def _save_failures(failures: dict[int, dict]) -> None:
    FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAILURES_FILE.write_text(
        json.dumps({str(k): v for k, v in failures.items()}, indent=2)
    )


def _record_failure(failures: dict[int, dict], beatport_id: int, error: str) -> None:
    entry = failures.get(beatport_id) or {"attempts": 0}
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = error[:300]
    entry["last_attempt"] = datetime.now(timezone.utc).isoformat()
    failures[beatport_id] = entry


def _clear_failure(failures: dict[int, dict], beatport_id: int) -> None:
    failures.pop(beatport_id, None)


# ── DJ Studio process guard ───────────────────────────────────────────────────

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


# ── DJ Studio access JWT ──────────────────────────────────────────────────────

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


# ── Result shaping (helper output → analysis fields) ──────────────────────────

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
    """Decode the Node helper's per-track output into our analysis schema.

    Returns None if the cf.dj.studio classifier rejected the request, returned
    invalid license, or produced a key/energy outside our valid ranges. The
    caller should record this as a failure (sidecar increments attempts).
    """
    server = result.get("server") or {}
    if not server.get("ok"):
        return None
    body = server.get("body") or {}
    if not body.get("IsLicenseValid"):
        return None

    key_summary = body.get("KeySummary") or {}
    main_key = key_summary.get("MainKey")
    second_key = key_summary.get("SecondKey")
    main_confidence = key_summary.get("MainKeyConfidence")
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

    # Phrase numbering: 8-bar (32-beat) groups anchored to first downbeat.
    # DJ Studio's own track-structures phraseData stays empty (the renderer
    # never calls the phrase ML model), so we synthesize this for richer
    # per-beat metadata in beat_data without claiming it as DJ Studio output.
    first_downbeat_ix = next(
        (i for i, b in enumerate(beats) if b.get("position", 1) == 1),
        0,
    )
    PHRASE_BEATS = 32

    def _energy_seg_for_beat(t: float) -> int:
        for s in energy_level_segments:
            if s["startTime"] <= t < s["endTime"]:
                return s["nr"]
        return 0

    beat_data: list[dict] = []
    for nr, b in enumerate(beats):
        offset = max(0, nr - first_downbeat_ix)
        phrase_nr = offset // PHRASE_BEATS
        beat_t = b.get("time", 0)
        beat_data.append({
            "nr": nr,
            "time": beat_t,
            "originalTime": beat_t,
            "type": -1 if (offset % 8 == 0 and offset > 0) else 0,
            "phraseNr": phrase_nr,
            "energyLevelNr": _energy_seg_for_beat(beat_t),
        })

    cue_points_count = len(cue_points)

    # Stem metrics passed through from the Node helper.
    stem_metrics = result.get("stem_metrics") or {}
    def _sm(stem: str, field: str) -> Optional[float]:
        return (stem_metrics.get(stem) or {}).get(field)

    # Per-bucket RMS series from the helper. Each is base64 of a uint16 LE
    # array; the helper used 1024-sample buckets at 44.1k → ~23ms per bucket.
    # Decode + downsample into per-second curves and per-energy-segment averages
    # so the LLM blob has phrase-level + curve-level stem signal without
    # ballooning the JSON. Curves: 1Hz (~300 floats per stem for a 5-min track).
    stems_rms_b64 = result.get("stems_rms_per_bucket_b64") or {}
    bucket_samples = int(result.get("stems_bucket_samples") or 1024)
    target_sr = int(result.get("stems_target_sr") or 44100)
    bucket_dur = bucket_samples / target_sr  # ≈ 0.02322 sec
    buckets_per_sec = 1.0 / bucket_dur       # ≈ 43.06

    def _decode_bucket_rms(b64: Optional[str]) -> list[float]:
        if not b64:
            return []
        import base64 as _b64
        import array as _arr
        raw = _b64.b64decode(b64)
        arr = _arr.array("H")
        arr.frombytes(raw)
        return [v / 65535.0 for v in arr]

    def _downsample_to_1hz(rms: list[float]) -> list[float]:
        if not rms:
            return []
        win = max(1, int(round(buckets_per_sec)))
        out: list[float] = []
        for i in range(0, len(rms), win):
            chunk = rms[i:i + win]
            if chunk:
                out.append(sum(chunk) / len(chunk))
        return out

    def _segment_stats(rms: list[float], start_sec: float, end_sec: float) -> dict:
        if not rms or end_sec <= start_sec:
            return {"avg_rms": None, "peak_rms": None}
        i0 = int(start_sec / bucket_dur)
        i1 = int(end_sec / bucket_dur)
        i0 = max(0, min(i0, len(rms)))
        i1 = max(i0, min(i1, len(rms)))
        if i0 == i1:
            return {"avg_rms": None, "peak_rms": None}
        win = rms[i0:i1]
        return {"avg_rms": sum(win) / len(win), "peak_rms": max(win)}

    _stem_rms = {stem: _decode_bucket_rms(stems_rms_b64.get(stem)) for stem in ("vocals", "drums", "bass", "other")}
    _stem_curve_1hz = {stem: _downsample_to_1hz(arr) for stem, arr in _stem_rms.items()}
    _stem_per_segment = {
        stem: [
            _segment_stats(_stem_rms[stem], s["startTime"], s["endTime"])
            for s in energy_level_segments
        ]
        for stem in _stem_rms
    }

    rich = {
        "mik_key_secondary": str(second_key) if second_key else None,
        "mik_key_confidence": (
            float(main_confidence) if main_confidence is not None else None
        ),
        "tempo_precise": float(bpm) if bpm else None,
        "duration_sec": duration_sec or None,
        "phrase_count": None,  # set by rekordbox PSSI ingest later
        "cue_points_count": cue_points_count,
        "vocals_avg":  _sm("vocals", "avg_rms"),
        "drums_avg":   _sm("drums",  "avg_rms"),
        "bass_avg":    _sm("bass",   "avg_rms"),
        "melody_avg":  _sm("other",  "avg_rms"),    # Demucs "other" → melody
        "vocals_peak": _sm("vocals", "peak_rms"),
        "drums_peak":  _sm("drums",  "peak_rms"),
        "bass_peak":   _sm("bass",   "peak_rms"),
        "melody_peak": _sm("other",  "peak_rms"),
    }

    # Compact analysis blob for LLM consumption.
    analysis = {
        "version": 1,
        "key": {
            "main": main_key,
            "main_int": mik_key_int,
            "main_confidence": main_confidence,
            "second": second_key,
            "second_confidence": key_summary.get("SecondKeyConfidence"),
            "is_single_note": key_summary.get("MainKeyIsSingleNote"),
        },
        "energy": {
            "overall": mik_nrg_int,
            "segments": [
                {
                    "start_beat": s["startBeatNr"],
                    "beat_length": s["beatLength"],
                    "start_sec": s["startTime"],
                    "end_sec": s["endTime"],
                    "energy": s["mikEnergy"],
                    "label": s["label"],
                    "volume_rms_db": s["mikVolume"],
                }
                for s in energy_level_segments
            ],
        },
        "cue_points": [
            {"beat": cp["beat"], "time_sec": cp["time"], "type": cp["type"]}
            for cp in cue_points
        ],
        "tempo": {
            "bpm": bpm,
            "wasm_tempo": result.get("wasm", {}).get("tempo"),
            "downbeat_time_sec": result.get("wasm", {}).get("downbeat_time"),
            "cue_point_start_beat": result.get("wasm", {}).get("cue_point_start_beat"),
        },
        "structure": {
            "beats": len(beats),
            "first_downbeat_beat_ix": first_downbeat_ix,
        },
        "stems": {
            stem: {
                "avg_rms":  _sm(stem, "avg_rms"),
                "peak_rms": _sm(stem, "peak_rms"),
                # 1Hz curve: one mean RMS value per second of audio. Use to find
                # where each stem rises/falls ("vocals come in around 60s").
                "curve_1hz": _stem_curve_1hz[stem],
                # Per-energy-segment averages — index-aligned with energy.segments[].
                # Use to characterise phrases ("drop has high drums + bass, low vocals").
                "per_segment": _stem_per_segment[stem],
            }
            for stem in ("vocals", "drums", "bass", "other")
        },
        "stems_meta": {
            "bucket_dur_sec": bucket_dur,
            "curve_hz": 1,
        },
    }

    return {
        "bpm": bpm,
        "duration_sec": duration_sec,
        "mik_key_int": mik_key_int,
        "mik_nrg_int": mik_nrg_int,
        "cue_points": cue_points,
        "energy_level_segments": energy_level_segments,
        "beat_data": beat_data,
        "rich": rich,
        "analysis_json": json.dumps(analysis, separators=(",", ":")),
    }


# ── Long-running Node helper ──────────────────────────────────────────────────

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

    def set_access_jwt(self, new_jwt: str) -> None:
        """Push a fresh DJ Studio access JWT down to the running Node helper.
        Subsequent analyze() calls pick it up automatically — no helper restart
        needed."""
        self._jwt = new_jwt
        self._send({"cmd": "setAccessJwt", "djsAccessJwt": new_jwt})
        while True:
            evt = self._read()
            if evt is None:
                return
            if evt.get("event") == "jwtUpdated":
                return
            if evt.get("event") == "log" and self._verbose:
                console.log(f"[dim]helper:[/dim] {evt.get('message')}")

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
                console.log(f"[red]helper error:[/red] {evt.get('message')}")
                return {"ok": False, "message": evt.get("message", "unknown")}
