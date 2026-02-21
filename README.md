# DJ Studio to Rekordbox Importer

Import mixes from DJ.Studio into rekordbox — playlists, transition metadata, and hot cue points for live performance.

## Architecture

```
 DJ.Studio (macOS)                mix.json                    rekordbox (encrypted DB)
 ─────────────────          ─────────────────────          ───────────────────────────
 projects-table/            Tracks + BPM/Key/Artist        Playlist (mix order)
 audio-library-table.json   Transitions (beats/effects)    Tracks (created if missing)
                            Beatport IDs                   Effects in Comment field
         |                         |                       Hot cues A-H per track
         v                         v                              ^
   get_mix_info.py  ──────>  JSON file  ──────>  import_to_rekordbox.py
                                                   Pass 1: tracks/playlist/effects
                                                   → analyze in rekordbox →
                                                   Pass 2 (--cues-only): snapped cues
```

## What It Does

- **Extracts** mix data from DJ.Studio's local database (tracks, order, transitions, BPM, key)
- **Matches** tracks to rekordbox by Beatport ID; creates missing ones as streaming entries (`FileType=20`)
- **Creates** a rekordbox playlist with tracks in mix order
- **Writes** transition effect names into each track's Comment field
- **Sets hot cue points** on each track, snapped to rekordbox's analyzed beatgrid for accurate placement

## Hot Cue Layout

Cue points follow the DJ.Studio convention so you can perform the mix on CDJs/controllers:

```
First track (5 cues):               Middle tracks (up to 8 cues):
  A = Play start                      B = Prep (x beats before incoming transition)
  B = Prep (x beats before out)       C = Incoming transition start
  C = Outgoing transition start       D = Incoming bass swap (if present)
  D = Outgoing bass swap (if any)     E = Incoming transition end
  E = Outgoing transition end         A = Prep (x beats before outgoing transition)
                                      F = Outgoing transition start
Last track (up to 4 cues):           G = Outgoing bass swap (if present)
  B/C/D/E = incoming only             H = Outgoing transition end
```

- **Prep distance (x)**: 8, 16, or 32 beats depending on transition length
- **Bass swap cue**: only set when `AE_Bass_Swap`, `AE_Bass_SwapFade`, or `AE_Bass_CrossFade` is present
- **Outgoing transition**: `end_beat - duration_beats` to `end_beat`
- **Incoming transition**: `start_beat` to `start_beat + duration_beats`

## Setup

```bash
cd /path/to/dj
python3 -m venv .venv
.venv/bin/pip install pyrekordbox
```

## Quick Start

```bash
# 1. List available mixes
.venv/bin/python3 get_mix_info.py --list

# 2. Export a mix to JSON
.venv/bin/python3 get_mix_info.py "Ibiza Vibes" -o mix.json

# 3. Preview import (no changes)
.venv/bin/python3 import_to_rekordbox.py mix.json --dry-run

# 4. Close rekordbox, then run Pass 1 (creates tracks, playlist, effects)
.venv/bin/python3 import_to_rekordbox.py mix.json

# 5. Open rekordbox and let it analyze all tracks in the playlist

# 6. Close rekordbox, then run Pass 2 (writes cues snapped to beatgrid)
.venv/bin/python3 import_to_rekordbox.py mix.json --cues-only
```

## Scripts

### `get_mix_info.py` — Extract mix from DJ.Studio

Reads DJ.Studio's local database and audio library cache to produce a JSON file.

```bash
.venv/bin/python3 get_mix_info.py --list                    # List all mixes
.venv/bin/python3 get_mix_info.py "Mix Name"                # Show mix details
.venv/bin/python3 get_mix_info.py "Mix Name" --json         # Output as JSON
.venv/bin/python3 get_mix_info.py "Mix Name" -o file.json   # Export to file
```

**Data sources:**
- `~/Music/DJ.Studio/Database/projects-table/` — mix projects (track order, transitions, effects)
- `~/Music/DJ.Studio/Cache/Database/audio-library-table.json` — track metadata (title, artist, BPM, key, cue points)

**Output JSON structure:**
```json
{
  "metadata": { "name": "Ibiza Vibes", "track_count": 16, "bpm_min": 126, "bpm_max": 132 },
  "tracks": [
    { "position": 1, "title": "Pull Up", "artist": "Discip",
      "bpm": 129, "key": "11A", "start_beat": 64, "end_beat": 480,
      "library_key": "beatport-sdk_22866908" }
  ],
  "transitions": [
    { "number": 1, "duration_beats": 64, "effect_offset": 32,
      "effects": ["AE_CrossFade", "AE_Bass_CrossFade"] }
  ]
}
```

### `import_to_rekordbox.py` — Import into rekordbox DB

Writes directly into rekordbox's encrypted database via pyrekordbox. Bypasses XML import (which doesn't work for Beatport streaming tracks). Uses a two-pass workflow so cue points can be snapped to rekordbox's analyzed beatgrid.

```bash
# Pass 1: tracks, playlist, effects (no cues)
.venv/bin/python3 import_to_rekordbox.py mix.json
.venv/bin/python3 import_to_rekordbox.py mix.json --dry-run

# Pass 2: cues snapped to rekordbox's beatgrid
.venv/bin/python3 import_to_rekordbox.py mix.json --cues-only
.venv/bin/python3 import_to_rekordbox.py mix.json --cues-only --dry-run

# Pass 2 without snapping (fallback to raw beat positions)
.venv/bin/python3 import_to_rekordbox.py mix.json --cues-only --no-snap
```

**Pass 1** — create tracks and playlist:

1. **Match tracks** — looks up each Beatport ID via `FolderPath = /v4/catalog/tracks/{ID}/`
2. **Create missing tracks** — as Beatport streaming entries (`FileType=20`) with artist, genre, key, BPM
3. **Create playlist** — named after the mix
4. **Add tracks** — in correct mix order
5. **Write effects** — transition effect names stored in each track's Comment field

**Pass 2** (`--cues-only`) — write cues after rekordbox analysis:

6. **Read beatgrid** — loads ANLZ files and extracts the PQTZ beat grid (times in ms)
7. **Snap cues** — each cue position is snapped to the nearest beat in rekordbox's grid via binary search
8. **Write hot cues** — A-H cue points based on transition positions, aligned to rekordbox's beat grid
9. **Graceful fallback** — tracks without ANLZ data get unsnapped cues (reported as `[unsnapped]`)

## Requirements

- Python 3
- [pyrekordbox](https://github.com/dylanljones/pyrekordbox) (`pip install pyrekordbox`)
- Rekordbox must be **closed** before running the import

## Technical Details

### Beatport streaming tracks in rekordbox

- `FileType = 20`, `FolderPath = /v4/catalog/tracks/{BEATPORT_ID}/`
- `BPM` stored as integer * 100 (e.g., 129 BPM = 12900)
- `Commnt` field (note spelling) for transition effects
- `ExtInfo` contains `{"StreamingInfo": {"AudioQuality": "0", ...}}`

### Transition mapping

Transition N describes the mix between track N and track N+1:
- Track N gets `Trans out:` effects and outgoing cue points
- Track N+1 gets `Trans in:` effects and incoming cue points

### Hot cue implementation

- Cues written as `DjmdCue` entries with `Kind` 1-8 (A-H)
- Raw positions calculated from beat data: `beat * 60000 / bpm` = milliseconds
- In Pass 2, raw positions are snapped to the nearest beat in rekordbox's ANLZ beatgrid (PQTZ tag) using `bisect_left`
- Existing hot cues on a track are cleared before writing new ones
- Bass swap position uses `effect_offset` if > 0, otherwise transition midpoint

### Two-pass workflow

DJ.Studio and rekordbox analyze tracks independently, producing different beat grids. Cue points computed from DJ.Studio's beat positions can drift relative to rekordbox's grid. The two-pass approach fixes this:

1. **Pass 1** creates tracks/playlist/effects so rekordbox can see and analyze them
2. **Rekordbox analysis** generates ANLZ files with a PQTZ beat grid per track
3. **Pass 2** reads those beat grids and snaps each cue to the nearest analyzed beat
