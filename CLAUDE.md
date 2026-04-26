# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Three-script pipeline: `get_mix_info.py` extracts DJ.Studio mix data → `import_to_rekordbox.py` writes it into rekordbox → `track_db.py` maintains a SQLite metadata DB (energy, vocals/drums/melody, section markers) seeded from DJ.Studio's audio library.

## Commands

```bash
uv run get_mix_info.py --list
uv run get_mix_info.py "Mix Name" -o mix.json

# Track metadata database (energy, vocals/drums/melody, section markers):
uv run track_db.py populate                        # seed from DJ Studio library
uv run track_db.py list
uv run track_db.py show beatport-sdk_12345678
uv run track_db.py update beatport-sdk_12345678 --energy 8 --vocals high --drums high --melody low
uv run track_db.py section add beatport-sdk_12345678 intro 0 64
uv run track_db.py section add beatport-sdk_12345678 drop 128 256
uv run track_db.py section list beatport-sdk_12345678

# Two-pass import workflow:
# Pass 1: create tracks, playlist, effects (no cues)
uv run import_to_rekordbox.py mix.json --dry-run
uv run import_to_rekordbox.py mix.json
# → Open rekordbox, analyze all tracks, close rekordbox
# Pass 2: write cues snapped to rekordbox's beatgrid
uv run import_to_rekordbox.py mix.json --cues-only --dry-run
uv run import_to_rekordbox.py mix.json --cues-only
# Pass 2 without snapping (fallback to raw beat positions)
uv run import_to_rekordbox.py mix.json --cues-only --no-snap

# Setup
uv sync
```

Run tests: `uv run pytest`. Use `--dry-run` to verify import behavior without writing to the database.

## Architecture

**`get_mix_info.py`** reads DJ.Studio's local files and produces a JSON intermediate format:
- `~/Music/DJ.Studio/Database/projects-table/{uuid}` — mix project data (track order, transitions with duration/effects/offset)
- `~/Music/DJ.Studio/Database/audio-library-table/{hash_prefix}/{library_key}` — sharded track metadata (BPM, key, cue points with `start_beat`/`end_beat`)
- Converts DJ.Studio numeric keys to Camelot notation via a hardcoded map

**`import_to_rekordbox.py`** reads that JSON and writes into rekordbox's encrypted SQLite via `pyrekordbox.Rekordbox6Database`:
1. Matches tracks by Beatport ID (`FolderPath = /v4/catalog/tracks/{ID}/`)
2. Creates missing tracks as `FileType=20` streaming entries
3. Creates a playlist, adds tracks in order
4. Writes transition effects to `Commnt` field (note: rekordbox uses this spelling)
5. (Pass 2, `--cues-only`) Reads ANLZ beatgrids, snaps cue points, writes `DjmdCue` entries (`Kind` 1-8 = pads A-H)

## Key Design Decisions

- **Direct DB writes via pyrekordbox** — XML import doesn't work for Beatport streaming tracks, so we write to `master.db` directly. Rekordbox must be closed.
- **Hot cue layout** — A-D = incoming transition (A=prep, B=start, C=bass swap, D=end). E-H = outgoing transition (E=prep, F=start, G=bass swap, H=end). Letters left empty when transition or bass swap doesn't exist.
- **Outgoing transition direction** — Starts AT `end_beat` and extends forward by `duration_beats` (not backward). Incoming starts at `start_beat` and extends forward.
- **Prep cue distance** — `PREP_BARS` (default 8) bars before transition start. Configurable class constant.
- **Bass swap cue** — Only written when `AE_Bass_Swap`, `AE_Bass_SwapFade`, or `AE_Bass_CrossFade` is in the effects list. Position uses `effect_offset` if > 0, else transition midpoint.
- **Two-pass import** — Pass 1 creates tracks/playlist/effects but skips cues. After rekordbox analyzes the tracks (generating ANLZ beatgrids), Pass 2 (`--cues-only`) reads the PQTZ tag from ANLZ files and snaps cue points to the nearest downbeat (beat 1 of bar) via binary search (`bisect_left`). `--no-snap` disables snapping for fallback.
- **Beat-to-ms conversion** — `beat * 60000 / bpm`. Uses each track's own BPM.
- **Transition numbering** — Transition N = mix between track N and track N+1. Outgoing = `trans_by_num[pos]`, incoming = `trans_by_num[pos-1]`.
- **BPM storage** — Rekordbox stores BPM as integer * 100 (129 BPM = 12900).
- **Cue IDs** — Generated via `uuid4()`, not `generate_unused_id`, because `DjmdCue` uses string UUIDs.
