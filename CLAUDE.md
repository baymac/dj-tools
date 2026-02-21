# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Two-script pipeline that imports DJ.Studio mixes into rekordbox's encrypted database via pyrekordbox. Handles Beatport streaming tracks, transition metadata, and hot cue points for live performance.

## Commands

```bash
# Run with venv (pyrekordbox requires it)
.venv/bin/python get_mix_info.py --list
.venv/bin/python get_mix_info.py "Mix Name" -o mix.json

# Two-pass import workflow:
# Pass 1: create tracks, playlist, effects (no cues)
.venv/bin/python import_to_rekordbox.py mix.json --dry-run
.venv/bin/python import_to_rekordbox.py mix.json
# → Open rekordbox, analyze all tracks, close rekordbox
# Pass 2: write cues snapped to rekordbox's beatgrid
.venv/bin/python import_to_rekordbox.py mix.json --cues-only --dry-run
.venv/bin/python import_to_rekordbox.py mix.json --cues-only
# Pass 2 without snapping (fallback to raw beat positions)
.venv/bin/python import_to_rekordbox.py mix.json --cues-only --no-snap

# Setup
python3 -m venv .venv
.venv/bin/pip install pyrekordbox
```

No test suite exists. Use `--dry-run` to verify import behavior without writing to the database. Syntax check: `.venv/bin/python -c "import py_compile; py_compile.compile('import_to_rekordbox.py', doraise=True)"`.

## Architecture

**`get_mix_info.py`** reads DJ.Studio's local files and produces a JSON intermediate format:
- `~/Music/DJ.Studio/Database/projects-table/{uuid}` — mix project data (track order, transitions with duration/effects/offset)
- `~/Music/DJ.Studio/Cache/Database/audio-library-table.json` — track metadata (BPM, key, cue points with `start_beat`/`end_beat`)
- Converts DJ.Studio numeric keys to Camelot notation via a hardcoded map

**`import_to_rekordbox.py`** reads that JSON and writes into rekordbox's encrypted SQLite via `pyrekordbox.Rekordbox6Database`:
1. Matches tracks by Beatport ID (`FolderPath = /v4/catalog/tracks/{ID}/`)
2. Creates missing tracks as `FileType=20` streaming entries
3. Creates a playlist, adds tracks in order
4. Writes transition effects to `Commnt` field (note: rekordbox uses this spelling)
5. (Pass 2, `--cues-only`) Reads ANLZ beatgrids, snaps cue points, writes `DjmdCue` entries (`Kind` 1-8 = pads A-H)

## Key Design Decisions

- **Direct DB writes via pyrekordbox** — XML import doesn't work for Beatport streaming tracks, so we write to `master.db` directly. Rekordbox must be closed.
- **Hot cue layout** — First track: A=play start, B-E=outgoing transition. Middle tracks: B-E=incoming, A/F-H=outgoing. Last track: B-E=incoming only.
- **Prep cue distance** — 8, 16, or 32 beats before transition start, bucketed by `get_prep_beats(duration_beats)`.
- **Bass swap cue** — Only written when `AE_Bass_Swap`, `AE_Bass_SwapFade`, or `AE_Bass_CrossFade` is in the effects list. Position uses `effect_offset` if > 0, else transition midpoint.
- **Two-pass import** — Pass 1 creates tracks/playlist/effects but skips cues. After rekordbox analyzes the tracks (generating ANLZ beatgrids), Pass 2 (`--cues-only`) reads the PQTZ tag from ANLZ files and snaps cue points to the nearest beat in rekordbox's grid via binary search (`bisect_left`). `--no-snap` disables snapping for fallback.
- **Beat-to-ms conversion** — `beat * 60000 / bpm`. Uses each track's own BPM.
- **Transition numbering** — Transition N = mix between track N and track N+1. Outgoing = `trans_by_num[pos]`, incoming = `trans_by_num[pos-1]`.
- **BPM storage** — Rekordbox stores BPM as integer * 100 (129 BPM = 12900).
- **Cue IDs** — Generated via `uuid4()`, not `generate_unused_id`, because `DjmdCue` uses string UUIDs.
