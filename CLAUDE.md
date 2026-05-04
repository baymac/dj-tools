# CLAUDE.md

Guidance to Claude Code for this repository.

## Project Overview

Unified DJ tool. Builds a fully-analysed track library by progressively enriching each track with Beatport metadata, DJ Studio analysis, and rekordbox phrase tags. Then any SQL-curated subset can be pushed to a Beatport playlist, a rekordbox playlist, or a DJ Studio mix.

All tool-generated files live under `~/Music/dj-tools/` (DB at `dj.db`, per-command logs at `logs/<cmd>/`, state at `state/`, etc). `paths.py` is the single source of truth and auto-migrates old locations on first import.

See `README.md` for the full pipeline narrative + flag reference. This file only covers things that aren't obvious from reading the code.

## Layout

```
dj_cli.py                       CLI entrypoint — detect / sync / playlist / login-beatport

connections/                    Transport layer (no app-specific deps)
  beatport.py                   Beatport HTTP client + Playwright session token capture
  matching.py                   Fuzzy title/artist matching
  musickit.py                   Swift MusicKit bridge subprocess wrapper
  bridge/musickit_bridge.swift  Compiled on first use, cached

detect/                         Track detection + enrichment pipeline (Stages 2-6)
  db.py                         All detect+enrich DB operations
  cli.py                        argparse subcommands + async dispatch
  enrich.py                     Stage 3: detected → Beatport metadata (also full track-detail)
  sync_beatport.py              Stage 4: pull Beatport library → enriched_tracks_full
  import_to_studio.py           Stage 5a: drive DJ Studio SDK headlessly
  dj_studio_sdk.js              Long-running Node helper for Stage 5a
  enrich_studio.py              Stage 5b: read DJ Studio library files
  export_to_rekordbox.py        Stage 6a: idempotent pending → rekordbox playlist
  import_rekordbox_analysis.py  Stage 6b: ingest PSSI + cues from ANLZ
  instagram.py / mixcloud.py / youtube.py / radio.py / podbean.py / reddit.py
                                Stage 2: per-platform Shazam capture
  shazam.py / parser.py         Audio recognition + tracklist parsing

sync/                           Stage 1: Apple Music → Beatport
  db.py / sync.py / classifier.py / cli.py

playlist/                       SQL-curated push to one of three destinations
  query.py                      Run user SQL → list[beatport_id] + full-row fetch
  to_beatport.py                Push to a Beatport playlist
  to_rekordbox.py               Push to a rekordbox playlist (also called by Stage 6a)
  to_djstudio.py                Write a DJ Studio mix project file
  cli.py

djstudio/                       Read DJ Studio's local files
  extractor.py                  audio-library-table loader (used by enrich-studio)
  keys.py                       Camelot conversion

rekordbox/                      Rekordbox writes via pyrekordbox
  backup.py                     master.db backup before any write
  constants.py                  Path discovery + Camelot/cue-kind constants

helpers/                        Standalone maintenance scripts
tests/                          pytest
```

## Commands

```bash
# Setup
uv sync
uv run playwright install chromium

# Tests
uv run pytest

# Auth
uv run dj_cli.py login-beatport          # auto / --ui / --cookie

# Pipeline (see README.md for full flow)
uv run dj_cli.py sync music-beatport sync --library
uv run dj_cli.py detect youtube <url>
uv run dj_cli.py detect enrich
uv run dj_cli.py detect sync-beatport
uv run dj_cli.py detect import-to-studio
uv run dj_cli.py detect enrich-studio
uv run dj_cli.py detect export-to-rekordbox
uv run dj_cli.py detect import-rekordbox-analysis

# SQL → playlist (any of the three destinations)
uv run dj_cli.py playlist beatport  --query "SELECT beatport_id FROM enriched_tracks_full WHERE ..." --name "..."
uv run dj_cli.py playlist rekordbox --query "..." --name "..."
uv run dj_cli.py playlist dj-studio --query "..." --name "..."

# Maintenance
uv run helpers/cleanup_playlist.py "Playlist Name" --dry-run
```

## Key Design Decisions

### Enrichment pipeline (detect + sync)

- **Two enriched tables** — `enriched_tracks` (lean legacy) + `enriched_tracks_full` (canonical). Stages 3 + 4 mirror lean rows into the full table on insert via `upsert_full_from_enrich`. Stages 5 and 6 only update the full table.
- **Beatport metadata in one place** — Full track-detail (label, ISRC, mix_name, sub_genre, length_ms, catalog_number) is fetched **only** in `detect/enrich.py`. Stages 5 and 6 must not call Beatport. Reason: `import-to-studio` already runs a long Node helper; an extra Beatport client + token-refresh path there is what caused the prior mid-run token-expiry incident.
- **Per-stage idempotency** — `enriched_tracks_full` carries `dj_studio_at`, `rekordbox_export_at`, `rekordbox_analysis_at`. Each stage's pending-query filters on its own `*_at IS NULL`. `--force` overrides.
- **DJ Studio analysis is headless via SDK** — `detect/import_to_studio.py` decrypts DJ Studio's local refresh token (AES-256-CBC, hardcoded key in `encryptedToken-v2.dat`), exchanges it for a JWT via `app-services.dj.studio`, then drives `dj_studio_sdk.js` (a long-running Node helper that loads `@appmachine/beatport-sdk` + `@appmachine/ai-stems` + `@appmachine/ai-beatgrid` + the MIK WASM extractor and calls `cf.dj.studio/mixedinkey/analyze`). DJ Studio must be quit (port 61894 + `.beatport/` cache locks).
- **No phrase labels from DJ Studio** — DJ Studio's `track-structures-table.phraseData` is always empty (the renderer never calls the dormant ML phrase model). Real semantic phrase labels (Intro/Verse/Chorus/Outro/Up/Down/Bridge) come exclusively from Stage 6's rekordbox PSSI tag.
- **Rekordbox round-trip = three steps** — `export-to-rekordbox` pushes bare Beatport streaming entries (`FileType=20`) into a named playlist with no cue points (those would shadow rekordbox's own analysis output). Manual: open rekordbox → right-click playlist → Analyze Tracks. Then `import-rekordbox-analysis` reads PSSI + PCO2/PCOB into `rk_analysis_json`.
- **insert_beatport_track mirror-write must run outside the outer txn** — Stage 4 writes to `enriched_tracks` inside a `with _connect()` write txn. The mirror-write to `enriched_tracks_full` opens its own `_connect()`. SQLite deadlocks if the inner connection runs while the outer holds a write lock — so the mirror call lives **after** the outer `with` exits. Don't fold it back inside.
- **Beatport access token refresh** — `BEATPORT_ACCESS_TOKEN` ~10 min, `BEATPORT_SESSION_TOKEN` ~32 days. The session token auto-refreshes the access token on 401. Don't add manual refresh wrappers around individual stages.
- **Stage 1 (sync) cursor** — `--library` mode tracks the last `library_added_date` processed in the `cursors` table; re-runs only handle new Apple Music additions. `synced_tracks` keeps per-track outcome (`added` / `duplicate` / `fuzzy_miss` / `no_classify`) so a track is never reprocessed.

### playlist (SQL → destination)

- **Stage 6a (`detect export-to-rekordbox`) and `playlist rekordbox` share the same core** — `playlist.to_rekordbox.push_to_rekordbox(rows, name)` does the writing. Stage 6a wraps it with the `rekordbox_export_at IS NULL` pending-query and an `on_added` callback to stamp the timestamp. `playlist rekordbox` calls the same function with no callback — pure ad-hoc curation, no pipeline-stamp side effects.
- **User SQL must return `beatport_id`** — `playlist.query.run_user_query` validates the query starts with `SELECT` and contains `beatport_id`, executes it, then re-fetches full rows from `enriched_tracks_full` (so columns like artist/title/genre/key/bpm/duration come from there regardless of which table the user queried). beatport_ids missing from `enriched_tracks_full` are reported and skipped.
- **DJ Studio mix file format** — `playlist dj-studio` writes to `~/Music/DJ.Studio/Database/projects-table/<uuid>` with linear `mixList` and empty `autoEffects`. The minimal field set is `{key, name, genre, duration, trackCount, minBpm, maxBpm, createdAt, lastModified, mixList, autoEffects}` — all of which DJ Studio's reader (`djstudio/extractor.py`) consumes. If DJ Studio rejects a generated mix on a future version, the missing field will surface in DJ Studio's logs; add it here.
- **dj-studio destination requires tracks already imported** — checks `~/Music/DJ.Studio/Database/audio-library-table/*/beatport-sdk_<id>` for each beatport_id; missing ones are warned and skipped. Run `dj detect import-to-studio` first to populate the library.
