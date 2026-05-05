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

from caffeinate import caffeinate
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

# Tracks shorter than this are reliably under ai-beatgrid's working window
# and Demucs needs a few seconds of audio to separate stems — skip them at
# queue time rather than burn ~30s/track only to commit empty data.
MIN_DURATION_MS = 30_000

# Persistent record of helper-level failures (helper.analyze returned ok=False
# OR _shape_result rejected the response). Tracks failing N consecutive times
# are auto-skipped on subsequent runs to avoid infinite recycle.
from paths import STATE_DIR  # noqa: E402

FAILURES_FILE = STATE_DIR / "import_to_studio_failures.json"
MAX_FAILURE_ATTEMPTS = 3


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


def _companion_file_paths(library_key: str) -> dict[str, Path]:
    """Where each per-track artifact lives in DJ Studio's filesystem."""
    shard = _shard(library_key)
    return {
        "structures": DJ_STUDIO_STRUCTURES / shard / library_key,
        "vocals":     DJ_STUDIO_VOCALS / shard / library_key,
        "drums":      DJ_STUDIO_DRUMS / shard / library_key,
        "bass":       DJ_STUDIO_BASS / shard / library_key,
        "melody":     DJ_STUDIO_MELODY / shard / library_key,
    }


def find_half_baked_library_entries() -> list[tuple[str, Path, list[str]]]:
    """Return library entries that have mikKey set but are missing companion
    files (track-structures or any of the 4 compressedAudioView*).

    Each tuple: (library_key, audio_library_table_path, [missing_artifact_names]).
    """
    half_baked: list[tuple[str, Path, list[str]]] = []
    if not DJ_STUDIO_LIBRARY.is_dir():
        return half_baked
    for shard in DJ_STUDIO_LIBRARY.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.iterdir():
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            k = data.get("key")
            if not k:
                continue
            if data.get("mikKey") is None and data.get("camelotKey") is None:
                continue  # no mikKey → already not in skip set, will reprocess
            companions = _companion_file_paths(k)
            missing = [name for name, p in companions.items() if not p.is_file()]
            if missing:
                half_baked.append((k, f, missing))
    return half_baked


def _library_keys_used_in_mixes() -> set[str]:
    """Return every libraryKey referenced by any mix in DJ Studio's
    projects-table. Deleting entries in this set would leave broken slots
    in your saved mixes."""
    used: set[str] = set()
    projects_dir = DJ_STUDIO_DB / "projects-table"
    if not projects_dir.is_dir():
        return used
    for f in projects_dir.iterdir():
        if not f.is_file():
            continue
        try:
            project = json.loads(f.read_text())
        except Exception:
            continue
        for ref in project.get("mixList") or []:
            lk = ref.get("libraryKey") if isinstance(ref, dict) else None
            if lk:
                used.add(lk)
    return used


def repair_studio_library(*, dry_run: bool = False, include_orphans: bool = False) -> dict:
    """Find + delete half-baked DJ Studio library entries so import-to-studio
    can re-process them.

    Three classifications:
    - recoverable: beatport_id is in `enriched_tracks`. Next import-to-studio
      will re-queue. Deleted by default.
    - orphan (free): NOT in enriched_tracks AND NOT referenced by any mix.
      Deleted only with `include_orphans=True` (no recovery path through this
      tool, but no in-use mixes affected either).
    - orphan (in-use): NOT in enriched_tracks BUT referenced by at least one
      mix in projects-table. NEVER deleted — would leave a broken slot in
      your saved mix. Reported only.
    """
    half_baked = find_half_baked_library_entries()
    counts = {
        "scanned": 0, "half_baked": len(half_baked),
        "recoverable": 0, "orphan_free": 0, "orphan_in_use": 0, "removed": 0,
    }
    if not DJ_STUDIO_LIBRARY.is_dir() or not half_baked:
        return counts
    counts["scanned"] = sum(
        1 for shard in DJ_STUDIO_LIBRARY.iterdir() if shard.is_dir()
        for _ in shard.iterdir()
    )

    import sqlite3
    con = sqlite3.connect(detect_db.DB_PATH)
    enriched_bp_ids: set[int] = {
        r[0] for r in con.execute(
            "SELECT DISTINCT beatport_id FROM enriched_tracks WHERE beatport_id IS NOT NULL"
        )
    }
    con.close()
    in_use_keys = _library_keys_used_in_mixes()

    for library_key, lib_path, missing in half_baked:
        try:
            bp = int(library_key.removeprefix(f"{KIND}_"))
        except ValueError:
            bp = None
        recoverable = bp is not None and bp in enriched_bp_ids
        in_use = library_key in in_use_keys

        if recoverable:
            tag = "recoverable"
            counts["recoverable"] += 1
            should_remove = True
        elif in_use:
            tag = "orphan-in-use"
            counts["orphan_in_use"] += 1
            should_remove = False  # never auto-delete: would break a saved mix
        else:
            tag = "orphan-free"
            counts["orphan_free"] += 1
            should_remove = include_orphans

        if not should_remove:
            reason = "referenced by a saved mix — would break the slot" if in_use else "orphan, no enriched_tracks row"
            console.print(f"[dim]skip ({tag}, {reason})[/dim]  {library_key}")
            continue
        if dry_run:
            console.print(f"[yellow]would remove ({tag})[/yellow]  {library_key}  missing: {','.join(missing)}")
        else:
            try:
                lib_path.unlink()
                counts["removed"] += 1
                console.print(f"[red]removed ({tag})[/red]  {library_key}  missing: {','.join(missing)}")
            except OSError as e:
                console.print(f"[red]failed to remove[/red]  {library_key}: {e}")
    return counts


def _existing_library_keys() -> set[str]:
    """Return library_keys for entries that have completed analysis.

    DJ Studio's filesystem is the single source of truth for "is this track
    already imported AND analysed". An entry counts as done only if `mikKey`
    (or its alias `camelotKey`) is set — entries written before cf.dj.studio
    classified them have those fields NULL and should be retried.
    """
    keys: set[str] = set()
    if not DJ_STUDIO_LIBRARY.is_dir():
        return keys
    for shard in DJ_STUDIO_LIBRARY.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.iterdir():
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            k = data.get("key")
            if not k:
                continue
            if data.get("mikKey") is None and data.get("camelotKey") is None:
                continue  # entry exists but cf.dj.studio never classified it
            keys.add(k)
    return keys


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
    """Pull DJ-Studio-shaped fields + rich analysis fields out of the Node helper output."""
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

    # Phrase numbering: DJ Studio uses an 8-bar (32-beat) rule, anchored to the
    # first downbeat (position == 1). Each beat gets phraseNr = (offset // 32)
    # where offset is the beat-count from the first downbeat. (Beats before the
    # first downbeat keep phraseNr = 0.) This matches what we observed in real
    # DJ Studio audio-library-table entries: phraseData stays [], but beatData
    # contains phrase indices 0..N-1 covering ~32 beats each.
    first_downbeat_ix = next(
        (i for i, b in enumerate(beats) if b.get("position", 1) == 1),
        0,
    )
    PHRASE_BEATS = 32

    # Each beat also belongs to an energyLevelSegment. Look up by time bucket.
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
            # Real DJ Studio beatData: type = 0 for normal beats, -1 every ~8 beats
            # (it appears to mark mid-bar accent points, not downbeats). We mirror
            # that by stamping -1 every 8th beat from the first downbeat.
            "type": -1 if (offset % 8 == 0 and offset > 0) else 0,
            "phraseNr": phrase_nr,
            "energyLevelNr": _energy_seg_for_beat(beat_t),
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

    # Phrase count: max phraseNr + 1 (or 0 if no beats)
    phrase_count = max((b["phraseNr"] for b in beat_data), default=-1) + 1
    cue_points_count = len(cue_points)

    # Stem metrics passed through from the Node helper.
    stem_metrics = result.get("stem_metrics") or {}
    def _sm(stem: str, field: str) -> Optional[float]:
        return (stem_metrics.get(stem) or {}).get(field)

    # phrase_count would be a count of our own 8-bar groupings, not a number
    # DJ Studio publishes anywhere — leave it None until we wire in rekordbox's
    # PSSI phrase data (which gives a real labelled count).
    rich = {
        "mik_key_secondary": str(second_key) if second_key else None,
        "mik_key_confidence": (
            float(main_confidence) if main_confidence is not None else None
        ),
        "tempo_precise": float(bpm) if bpm else None,
        "duration_sec": duration_sec or None,
        "phrase_count": None,
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

    # No per-phrase array. DJ Studio doesn't produce one (its renderer never
    # calls the phrase ML model; real audio-library-table entries we examined
    # have phraseData=[] empty), and we refuse to invent labels or per-phrase
    # stem stats by sliding our own windows over the audio.
    #
    # TODO: when we wire up rekordbox's PSSI phrase tags, those carry real
    # labelled boundaries (Intro / Verse / Pre-Chorus / Chorus / Bridge / Outro
    # — or Mood-3 EDM variant Intro/Up/Down/Chorus/Drop/Outro). Only works for
    # tracks already imported AND analyzed in rekordbox.

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
                "avg_rms": _sm(stem, "avg_rms"),
                "peak_rms": _sm(stem, "peak_rms"),
            }
            for stem in ("vocals", "drums", "bass", "other")
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
        "bpm_line": bpm_line,
        "beat_grids": beat_grids,
        "stems_compressed_b64": result.get("stems_compressed_b64") or {},
        "rich": rich,
        "analysis_json": json.dumps(analysis, separators=(",", ":")),
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
    # Build phraseData from the per-beat phraseNr we already computed.
    # Each phrase entry: {nr, startBeatNr, beatLength}.
    phrase_data: list[dict] = []
    if shaped["beat_data"]:
        cur = shaped["beat_data"][0]["phraseNr"]
        start_nr = 0
        for i, bd in enumerate(shaped["beat_data"]):
            if bd["phraseNr"] != cur:
                phrase_data.append({
                    "nr": cur,
                    "startBeatNr": start_nr,
                    "beatLength": i - start_nr,
                })
                cur = bd["phraseNr"]
                start_nr = i
        phrase_data.append({
            "nr": cur,
            "startBeatNr": start_nr,
            "beatLength": len(shaped["beat_data"]) - start_nr,
        })

    entry = {
        "key": library_key,
        "beatData": shaped["beat_data"],
        "bpmLine": shaped["bpm_line"],
        "phraseData": phrase_data,
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

    def set_access_jwt(self, new_jwt: str) -> None:
        """Push a fresh DJ Studio access JWT down to the running Node helper.
        Subsequent analyze() calls pick it up automatically — no helper restart
        needed."""
        self._jwt = new_jwt
        self._send({"cmd": "setAccessJwt", "djsAccessJwt": new_jwt})
        # Drain until ack so we don't race with the next analyze()
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
                # Helper-level error (not track-specific) — still surface it
                console.log(f"[red]helper error:[/red] {evt.get('message')}")
                return {"ok": False, "message": evt.get("message", "unknown")}


# ── 5. Top-level runner ───────────────────────────────────────────────────────

def run_import_to_studio(
    *,
    limit: int = 0,
    verbose: bool = False,
    force: bool = False,
    retry_failed: bool = False,
) -> None:
    from paths import command_logger
    with command_logger("import-to-studio", console) as log_path, caffeinate():
        console.print(f"[dim]Log: {log_path}[/dim]")
        _run_import_to_studio_impl(
            limit=limit, verbose=verbose, force=force, retry_failed=retry_failed,
        )


def _run_import_to_studio_impl(
    *, limit: int, verbose: bool, force: bool, retry_failed: bool,
) -> None:
    if is_dj_studio_running():
        console.print(
            "[red]DJ Studio is currently running.[/red]\n"
            "Quit DJ.Studio (Cmd+Q) before running this command — its SDK conflicts "
            "with our pipeline (port 61894 + cache file locks)."
        )
        return

    console.print("[bold]import-to-studio[/bold]  (Path A: full tracks via DJ Studio SDK)")

    candidates = detect_db.get_import_to_studio_pending(force=force)

    library_keys = _existing_library_keys()
    failures = {} if retry_failed else _load_failures()
    if retry_failed:
        console.print("[dim]--retry-failed: ignoring hard-failure sidecar this run[/dim]")

    # Filter chain. force=True ignores all skip rules.
    skipped_in_library = skipped_short = skipped_too_many_failures = 0
    rows: list[dict] = []
    for r in candidates:
        bid = r["beatport_id"]
        if not force:
            if f"{KIND}_{bid}" in library_keys:
                skipped_in_library += 1
                continue
            length_ms = r["length_ms"] or 0
            if 0 < length_ms < MIN_DURATION_MS:
                skipped_short += 1
                continue
            entry = failures.get(bid)
            if entry and entry.get("attempts", 0) >= MAX_FAILURE_ATTEMPTS:
                skipped_too_many_failures += 1
                continue
        rows.append(dict(r))

    if limit:
        rows = rows[:limit]
    if not rows:
        console.print(
            "Nothing to import — every enriched track is already in DJ Studio's library "
            "(or below the duration / failure-attempt thresholds).\n"
            "[dim]Use --force to re-process all tracks; "
            "delete ~/Music/dj-tools/state/import_to_studio_failures.json to retry hard-failed tracks.[/dim]"
        )
        return
    console.print(
        f"{len(rows)} tracks queued{' [yellow](forced re-run)[/yellow]' if force else ''}.  "
        f"[dim]skipped: {skipped_in_library} already in library, "
        f"{skipped_short} short (<30s), "
        f"{skipped_too_many_failures} hard-failed ≥{MAX_FAILURE_ATTEMPTS}× before[/dim]"
    )

    try:
        access_jwt = _get_dj_studio_access_token()
    except Exception as e:
        console.print(f"[red]Failed to get DJ Studio access token: {e}[/red]")
        console.print(
            "[yellow]Open DJ Studio briefly to refresh its session, then quit and re-run.[/yellow]"
        )
        return

    counts = {"seen": 0, "ok": 0, "fail": 0, "retried": 0}
    failed_rows: list[dict] = []  # (row, last_error) for end-of-run retry pass

    def _is_auth_failure(err: str) -> bool:
        """cf.dj.studio rejected our JWT — the access token expired (60-min
        lifetime) or was invalidated. Surfaced by the Node helper as
        `classifier ok=False status=401 body={"message":"Signature is invalid"}`."""
        return "status=401" in err or "Signature is invalid" in err

    def _refresh_jwt_and_retry(row, helper, *, attempt_label: str) -> tuple[bool, str]:
        """Decrypt + re-exchange a fresh DJ Studio access JWT, push it down to
        the helper, and retry the track once."""
        nonlocal access_jwt
        try:
            access_jwt = _get_dj_studio_access_token()
        except Exception as e:
            return False, f"jwt refresh failed: {type(e).__name__}: {e}"
        helper.set_access_jwt(access_jwt)
        progress.log(
            f"[dim]Access JWT refreshed mid-run, retrying bp:{row['beatport_id']}…[/dim]"
        )
        return _process_one(row, helper, attempt_label=attempt_label)

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=console,
    )

    def _process_one(row, helper, *, attempt_label: str = "") -> tuple[bool, str]:
        """Returns (ok, error_message). Side-effect: writes to DJ Studio's local
        library files only — NOT to our DB. `dj detect enrich-studio` reads
        those files back and creates the enriched_tracks_analysis row."""
        bid = row["beatport_id"]
        artist = row["artist"] or ""
        title = row["title"] or ""

        res = helper.analyze(bid)
        if not res["ok"]:
            return False, res.get("message", "unknown") or "unknown"

        shaped = _shape_result(bid, res["result"])
        if shaped is None:
            srv = (res.get("result") or {}).get("server") or {}
            return False, f"classifier ok={srv.get('ok')} status={srv.get('status')} body={str(srv.get('body'))[:120]}"

        # Save whatever we got. The classifier's mikKey + mikEnergy already
        # passed validation in _shape_result; that's enough to commit. We log
        # which subsystems came back empty (for repair-studio-library to
        # surface later), but don't refuse to write — partial data still has
        # value, and the alternative is recycling the track forever.
        stems_b64 = shaped.get("stems_compressed_b64") or {}
        partials = []
        if not shaped.get("beat_data") and not shaped.get("energy_level_segments"):
            partials.append("beats+energy")
        for stem_name, key in (("vocals", "vocals"), ("drums", "drums"), ("bass", "bass"), ("melody", "other")):
            if not stems_b64.get(key):
                partials.append(stem_name)
        if partials and verbose:
            progress.log(f"[yellow]bp:{bid} partial — missing {','.join(partials)} (saved anyway)[/yellow]")

        library_key = f"{KIND}_{bid}"
        entry = _build_library_entry(beatport_id=bid, artist=artist, title=title, shaped=shaped)

        # Write companion files first, library entry LAST. The library entry
        # (with mikKey set) is the skip indicator for `_existing_library_keys`.
        # If we get Ctrl-C'd between writes, the library entry won't be on disk
        # yet → next run reprocesses cleanly. Reverse order leaves half-baked
        # tracks marked "done" with missing stem data.
        _write_track_structures(library_key, shaped)
        stems_b64 = shaped["stems_compressed_b64"] or {}
        _write_compressed_view(DJ_STUDIO_VOCALS, library_key, stems_b64.get("vocals"))
        _write_compressed_view(DJ_STUDIO_DRUMS,  library_key, stems_b64.get("drums"))
        _write_compressed_view(DJ_STUDIO_BASS,   library_key, stems_b64.get("bass"))
        _write_compressed_view(DJ_STUDIO_MELODY, library_key, stems_b64.get("other"))
        _write_library_entry(library_key, entry)  # commit point — written last

        if verbose:
            t = res["result"].get("timing_ms", {})
            progress.log(
                f"[green]bp:{bid}[/green]{attempt_label}  "
                f"key={MIK_CAMELOT_INT_TO_STR.get(shaped['mik_key_int'])}/"
                f"{shaped['rich'].get('mik_key_secondary') or '-'}  "
                f"conf={shaped['rich'].get('mik_key_confidence') or 0:.2f}  "
                f"nrg={shaped['mik_nrg_int']}  "
                f"bpm={shaped['bpm']:.2f}  "
                f"phr={shaped['rich'].get('phrase_count')}  "
                f"cue={shaped['rich'].get('cue_points_count')}  "
                f"({t.get('total', 0)/1000:.1f}s)"
            )
        return True, ""

    with SdkHelper(access_jwt, verbose=verbose) as helper, progress:
        task = progress.add_task("Analyzing…", total=len(rows))
        for row in rows:
            counts["seen"] += 1
            artist = row["artist"] or ""
            title = row["title"] or ""
            progress.update(task, advance=1, description=f"{artist} — {title}")

            ok, err = _process_one(row, helper)
            if not ok and _is_auth_failure(err):
                ok, err = _refresh_jwt_and_retry(row, helper, attempt_label=" [post-refresh]")
                if not ok and _is_auth_failure(err):
                    progress.stop()
                    raise RuntimeError(
                        f"cf.dj.studio still rejecting our JWT after a fresh refresh. "
                        f"Wrote {counts['ok']}/{counts['seen']} tracks before the failure.\n\n"
                        f"This usually means encryptedToken-v2.dat itself is invalid "
                        f"(server-side session revoked, or the file is stale). Open DJ "
                        f"Studio, sign back in, quit (Cmd+Q), and re-run."
                    )
            if ok:
                counts["ok"] += 1
                _clear_failure(failures, row["beatport_id"])
            else:
                failed_rows.append({"row": row, "error": err})
                if verbose:
                    progress.log(f"[yellow]bp:{row['beatport_id']} first-pass failed:[/yellow] {err[:160]}")

        # Retry pass — failed rows often pass on a second try if cf.dj.studio
        # was momentarily slow. Wait briefly to let the service recover.
        if failed_rows:
            console.print(f"[dim]Retrying {len(failed_rows)} failed track(s) after 5s pause…[/dim]")
            import time as _t
            _t.sleep(5)
            retry_task = progress.add_task("Retrying…", total=len(failed_rows))
            still_failed: list[dict] = []
            for entry in failed_rows:
                row = entry["row"]
                progress.update(retry_task, advance=1,
                                description=f"{row['artist']} — {row['title']} (retry)")
                ok, err = _process_one(row, helper, attempt_label=" [retry]")
                if not ok and _is_auth_failure(err):
                    ok, err = _refresh_jwt_and_retry(row, helper, attempt_label=" [retry post-refresh]")
                    if not ok and _is_auth_failure(err):
                        progress.stop()
                        raise RuntimeError(
                            f"cf.dj.studio still rejecting our JWT after a fresh refresh during retry pass. "
                            f"Wrote {counts['ok']}/{counts['seen']} tracks before the failure.\n\n"
                            f"Open DJ Studio, sign back in, quit (Cmd+Q), and re-run."
                        )
                if ok:
                    counts["ok"] += 1
                    counts["retried"] += 1
                    _clear_failure(failures, row["beatport_id"])
                else:
                    counts["fail"] += 1
                    still_failed.append({"row": row, "error": err})
                    _record_failure(failures, row["beatport_id"], err)
                    if verbose:
                        progress.log(f"[red]bp:{row['beatport_id']} retry also failed:[/red] {err[:160]}")
            failed_rows = still_failed

    # Persist the failure sidecar so the next run can auto-skip tracks that
    # have hit MAX_FAILURE_ATTEMPTS.
    try:
        _save_failures(failures)
    except Exception as e:
        console.print(f"[yellow]Could not persist failure sidecar:[/yellow] {e}")

    console.print()
    summary = f"{counts['ok']}/{counts['seen']} written"
    if counts["retried"]:
        summary += f"  ([green]{counts['retried']} recovered on retry[/green])"
    if counts["fail"]:
        summary += f"  ([red]{counts['fail']} failed[/red])"
    console.print(f"[bold]Done.[/bold] {summary}")
    if failed_rows:
        console.print("[red]Permanently failed tracks (this run):[/red]")
        for fr in failed_rows:
            r = fr["row"]
            attempts = failures.get(r["beatport_id"], {}).get("attempts", 1)
            console.print(f"  bp:{r['beatport_id']} (attempt {attempts}/{MAX_FAILURE_ATTEMPTS}) — {r['artist']} — {r['title']}: {fr['error'][:160]}")
    if counts["ok"]:
        console.print("[dim]Next:[/dim] [cyan]uv run dj_cli.py detect enrich-studio[/cyan]")
