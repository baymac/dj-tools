# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A two-function CLI tool, plus a side bin of standalone analysis scripts.

**Function 1 — `dj_cli.py migrate`** moves a DJ.Studio mix into rekordbox. Extracts the project from DJ.Studio's local files, then writes tracks, playlist, transition effects, and beatgrid-snapped hot cues into rekordbox's encrypted SQLite via `pyrekordbox`.

**Function 2 — `dj_cli.py db`** maintains a SQLite metadata database (`~/Music/DJ.Studio/track_metadata.db`) seeded from DJ.Studio's library. Stores energy, vocals/drums/melody intensity, and section markers per track. Designed to feed an LLM-driven mixing recommender later.

**Side bin — `local-analyse/`** contains standalone helpers that aren't part of either pipeline: a single-track Beatport analyser (key/BPM/energy from preview audio, no DB writes) and the Beatport auth CLI it depends on.

## Layout

```
brasilia/
├── dj_cli.py                # Single CLI entrypoint: `migrate` and `db` subcommands
├── pipeline.py              # Full migrate flow: extract → Pass 1 → watch → Pass 2
├── djstudio/                # Read DJ.Studio's local files
│   ├── keys.py              # CAMELOT_MAP and Camelot conversion
│   ├── extractor.py         # DJStudioMixExtractor (projects, library, mixes)
│   └── display.py           # print_mix_info, print_mix_list
├── rekordbox/               # Write rekordbox's encrypted DB via pyrekordbox
│   ├── constants.py         # Paths, CUE_KIND, BASS_SWAP_EFFECTS, GENRE_PREP_BARS
│   ├── backup.py            # backup_db, undo_list, undo_restore
│   ├── cues.py              # Pure cue math: beats_to_ms, snap_to_beatgrid, prep_bars_for
│   ├── importer.py          # RekordboxImporter (Pass 1 + Pass 2 orchestrator)
│   └── display.py           # print_report, print_cues_report, fmt_ms
├── trackdb/                 # Track metadata DB
│   ├── schema.py            # SQLite schema + DB_PATH + validators
│   ├── library.py           # Read DJ.Studio library, structures, stems, projects
│   ├── commands.py          # cmd_populate / list / show / update / section_*
│   └── cli.py               # Build `dj db` subparsers + dispatch
├── helpers/                 # Maintenance scripts (not part of either main function)
│   └── cleanup_playlist.py  # Wipe a rekordbox playlist + cues + comments + tracks
├── local-analyse/           # Standalone helpers (not part of either pipeline)
│   ├── beatport_analyze.py  # Single Beatport URL → key+BPM+energy (read-only)
│   ├── beatport_auth.py     # Beatport login / status / clear
│   ├── djs_analyze.js       # ai-beatgrid wrapper for BPM
│   └── .beatport_token      # Stored token (gitignored)
├── tests/                   # pytest
├── logs/                    # gitignored
├── pyproject.toml
└── README.md
```

## Commands

```bash
# Function 1 — DJ Studio mix → Rekordbox
uv run dj_cli.py migrate "Mix Name"                  # full pipeline (extract + Pass 1 + watch + Pass 2)
uv run dj_cli.py migrate "Mix Name" --extract-only -o mix.json
uv run dj_cli.py migrate mix.json                    # use existing JSON
uv run dj_cli.py migrate mix.json --pass1-only --dry-run
uv run dj_cli.py migrate mix.json --pass2-only       # cues only (after rekordbox analysis)
uv run dj_cli.py migrate mix.json --no-watch         # Pass 1 only, skip the watch
uv run dj_cli.py migrate mix.json --no-snap          # Pass 2: don't snap to beatgrid
uv run dj_cli.py migrate --list                      # list available DJ.Studio mixes

# Undo — restore rekordbox DB from automatic pre-write backup
uv run dj_cli.py undo list
uv run dj_cli.py undo restore 20260427_143200_My_Mix.db

# Function 2 — Track metadata DB
uv run dj_cli.py db populate "Ibiza Vibes"           # tracks from latest revision of this mix
uv run dj_cli.py db list
uv run dj_cli.py db show beatport-sdk_12345678
uv run dj_cli.py db update beatport-sdk_12345678 --energy 8 --vocals high --drums high --melody low
uv run dj_cli.py db section add beatport-sdk_12345678 drop 128 256
uv run dj_cli.py db section list beatport-sdk_12345678
uv run dj_cli.py db section remove 5

# Standalone helpers (read-only, single-track)
uv run local-analyse/beatport_auth.py login
uv run local-analyse/beatport_auth.py status
uv run local-analyse/beatport_analyze.py https://www.beatport.com/track/title/12345678

# Maintenance — wipe a rekordbox playlist before re-running migrate
uv run helpers/cleanup_playlist.py --list
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --dry-run
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --delete-tracks

# Setup + tests
uv sync
uv run pytest
```

## Key Design Decisions

- **Direct DB writes via pyrekordbox** — XML import doesn't work for Beatport streaming tracks, so `rekordbox/importer.py` writes to `master.db` directly. Rekordbox must be closed.
- **Hot cue layout** — A-D = incoming transition (A=prep, B=start, C=bass swap, D=end). E-H = outgoing transition (E=prep, F=start, G=bass swap, H=end). Letters left empty when transition or bass swap doesn't exist.
- **Outgoing transition direction** — Starts AT `end_beat` and extends forward by `duration_beats` (not backward). Incoming starts at `start_beat` and extends forward.
- **Prep cue distance** — Genre-tuned via `GENRE_PREP_BARS` in `rekordbox/constants.py`: techno/trance=16, house/electronica=8, DnB/trap=4. Falls back to `PREP_BARS=8`. Always capped at half the transition duration so the prep cue doesn't collide with the previous transition's end cue.
- **Bass swap cue** — Only written when `AE_Bass_Swap`, `AE_Bass_SwapFade`, or `AE_Bass_CrossFade` is in the effects list. Position uses `effect_offset` if > 0, else transition midpoint.
- **Two-pass import** — Pass 1 creates tracks/playlist/effects but skips cues. After rekordbox analyzes the tracks (generating ANLZ beatgrids), Pass 2 reads the PQTZ tag from ANLZ files and snaps cue points to the nearest downbeat (beat 1 of bar) via `bisect_left`. `--no-snap` disables snapping for fallback.
- **Beat-to-ms conversion** — `beat * 60000 / bpm`. Uses each track's own BPM.
- **Transition numbering** — Transition N = mix between track N and track N+1. Outgoing = `trans_by_num[pos]`, incoming = `trans_by_num[pos-1]`.
- **BPM storage** — Rekordbox stores BPM as integer * 100 (129 BPM = 12900).
- **Cue IDs** — Generated via `uuid4()`, not `generate_unused_id`, because `DjmdCue` uses string UUIDs.
- **Rekordbox path discovery** — `rekordbox/constants.py` asks `pyrekordbox.config` for the actual master.db path (rekordbox 7 → 6 fallback). Don't hardcode the legacy `Application Support/Pioneer/rekordbox6/` path; rekordbox 7 lives at `~/Library/Pioneer/rekordbox/`.
- **Automatic backups** — Before every write (Pass 1, Pass 2, or `helpers/cleanup_playlist.py`), `backup_db()` copies `master.db` to `<db_dir>/claude-backups/{timestamp}_{mix}.db`. `dj_cli.py undo list` / `dj_cli.py undo restore` manage these. `cleanup_playlist.py` aborts if `backup_db()` returns `None`.
- **Mix name → latest revision** — DJ Studio saves a separate project file per revision, so the same mix name often points to many candidates. Both `migrate` and `db populate` use `djstudio.extractor.find_latest_project()`, which picks the candidate with the highest `lastModified` timestamp. The picked uuid + timestamp are printed so the caller can confirm.
- **`db populate` is single-mix** — Takes one required mix name. Inserts only tracks referenced by that mix's `mixList`. Existing rows from earlier populates stay in place (preserves user-edited annotations); to start clean, delete the SQLite file at `~/Music/DJ.Studio/track_metadata.db`.
- **ANLZ watcher** — `pipeline.watch_for_analysis()` counts `.DAT` files under `rekordbox6/share/` as a proxy for analysis progress (each track creates ~2 files). Advances to Pass 2 automatically once rekordbox closes.
- **`local-analyse/` is intentionally outside the main pipeline** — Beatport preview analysis is read-only and acts on a single track. It does not write to `track_metadata.db`. If you want analysis results in the DB, edit them in via `dj_cli.py db update`.
