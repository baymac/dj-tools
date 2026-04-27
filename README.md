# DJ CLI

A two-function CLI for moving DJ.Studio mixes into rekordbox and maintaining a per-track metadata database for live performance and future LLM-assisted mixing.

```
                                 dj_cli.py
                                 ┌────────────────────────┐
 DJ.Studio (macOS) ──────────►   │ migrate  ─► Rekordbox  │
 projects-table/                 │            playlist +  │
 audio-library-table/            │            cues + fx   │
                                 │                        │
 DJ.Studio library ──────────►   │ db       ─► SQLite     │
 track-structures-table/         │            energy +    │
 audio-library-stems/            │            sections    │
                                 └────────────────────────┘

 local-analyse/  (standalone helpers, not part of either pipeline)
   beatport_analyze.py  — single Beatport URL → key/BPM/energy (read-only)
   beatport_auth.py     — login / status / clear
```

## Setup

```bash
uv sync
```

Rekordbox must be **closed** before any `migrate` write.

## Function 1 — Migrate a mix into rekordbox

```bash
# Full pipeline: extract DJ.Studio JSON → Pass 1 (tracks + playlist + effects)
# → wait for rekordbox to analyze the playlist → Pass 2 (cues snapped to beatgrid)
uv run dj_cli.py migrate "Ibiza Vibes"

# Just extract the JSON, don't touch rekordbox
uv run dj_cli.py migrate "Ibiza Vibes" --extract-only -o mix.json

# Use existing JSON, skip extraction
uv run dj_cli.py migrate mix.json

# Just Pass 1 (tracks + playlist + effects, no cues yet)
uv run dj_cli.py migrate mix.json --pass1-only

# Just Pass 2 (cues, after rekordbox finished analyzing the playlist)
uv run dj_cli.py migrate mix.json --pass2-only

# Pass 2 fallback for tracks without ANLZ data
uv run dj_cli.py migrate mix.json --pass2-only --no-snap

# Preview what Pass 1 would do
uv run dj_cli.py migrate mix.json --pass1-only --dry-run

# List available DJ.Studio mixes
uv run dj_cli.py migrate --list

# Restore rekordbox's master.db from an automatic pre-write backup
uv run dj_cli.py undo list
uv run dj_cli.py undo restore 20260427_143200_Ibiza_Vibes.db
```

### Hot cue layout

```
Cue letters per track:
  A = Prep before incoming transition
  B = Incoming transition start
  C = Incoming bass swap (only if a bass-swap effect is present)
  D = Incoming transition end

  E = Prep before outgoing transition
  F = Outgoing transition start
  G = Outgoing bass swap (only if present)
  H = Outgoing transition end
```

Prep distance is genre-tuned: techno/trance = 16 bars, house/electronica = 8, DnB/trap = 4. Always capped at half the transition duration so the prep cue never collides with the previous transition's end cue.

## Function 2 — Track metadata DB

The SQLite DB lives at `~/Music/DJ.Studio/track_metadata.db`. Designed to feed an LLM mixing layer later.

```bash
# Seed from DJ.Studio for a specific mix (latest saved revision wins if duplicates).
# Imports tracks + MIK key + MIK energy + stem intensities + section markers.
uv run dj_cli.py db populate "Ibiza Vibes"

# List, inspect, edit
uv run dj_cli.py db list
uv run dj_cli.py db show beatport-sdk_12345678
uv run dj_cli.py db update beatport-sdk_12345678 --energy 8 --vocals high --drums high --melody low
uv run dj_cli.py db update beatport-sdk_12345678 --notes "afters peak track, big breakdown ~3min in"

# Section markers (intro / buildup / drop / breakdown / outro / bridge / verse / chorus)
uv run dj_cli.py db section add beatport-sdk_12345678 intro 0 64
uv run dj_cli.py db section add beatport-sdk_12345678 drop 128 256
uv run dj_cli.py db section list beatport-sdk_12345678
uv run dj_cli.py db section remove 5
```

## Local analysis helpers

Standalone scripts. Not part of the main pipeline. Read-only — they never write to the DB. Use them to spot-check a Beatport track before importing the mix.

```bash
# One-time login (Playwright headless browser captures Bearer token)
uv run local-analyse/beatport_auth.py login
uv run local-analyse/beatport_auth.py status
uv run local-analyse/beatport_auth.py clear

# Single-URL Beatport analysis: key (Krumhansl-Schmuckler chromagram), BPM (ai-beatgrid),
# energy (spectral brightness + onset density). Prints to stdout.
uv run local-analyse/beatport_analyze.py https://www.beatport.com/track/title/12345678
```

## Maintenance helpers

`helpers/` holds maintenance scripts that touch rekordbox but are not part of either
main function. The cleanup script wipes a previously imported playlist (cues +
comments + Beatport streaming entries) so you can rerun a migration cleanly.
Always backs up `master.db` first; aborts if the backup fails.

```bash
uv run helpers/cleanup_playlist.py --list                            # see playlists
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --dry-run           # preview
uv run helpers/cleanup_playlist.py "Ibiza Vibes"                     # remove playlist + cues + comments
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --delete-tracks     # also delete created tracks
```

## Tests

```bash
uv run pytest
```

## Architecture

See `CLAUDE.md` for the per-module layout, key design decisions, and the rekordbox DB schema notes (FileType=20 streaming entries, BPM × 100, ANLZ PQTZ beatgrid, etc.).
