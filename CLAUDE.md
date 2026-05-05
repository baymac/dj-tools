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
  sync_beatport.py              Stage 4: pull Beatport library → enriched_tracks
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
uv run dj_cli.py detect repair-studio-library --dry-run   # find half-baked DJ Studio entries
uv run dj_cli.py detect enrich-studio
uv run dj_cli.py detect export-to-rekordbox
uv run dj_cli.py detect import-rekordbox-analysis

# SQL → playlist (any of the three destinations)
uv run dj_cli.py playlist beatport  --query "SELECT beatport_id FROM enriched_tracks WHERE ..." --name "..."
uv run dj_cli.py playlist rekordbox --query "..." --name "..."
uv run dj_cli.py playlist dj-studio --query "..." --name "..."

# Maintenance
uv run helpers/cleanup_playlist.py "Playlist Name" --dry-run
```

## Key Design Decisions

### Enrichment pipeline (detect + sync)

- **Two enriched tables, no mirror** — `enriched_tracks` carries everything Beatport-derived (basic search-result fields + the 6 catalog-detail extras). `enriched_tracks_analysis` is sparse: keyed on `beatport_id`, holds only DJ Studio + rekordbox analysis data (mik_key, mik_nrg, vocals/drums/melody, rk_analysis_json) plus the per-stage timestamps. A row exists in the analysis table only after `enrich-studio` has populated it. Joins (e.g., `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)`) build the full picture at query time.
- **Beatport metadata in one place** — Full track-detail (label, ISRC, mix_name, sub_genre, length_ms, catalog_number) is fetched **only** in `detect/enrich.py` (and inline-extracted from playlist responses by `detect/sync_beatport.py`) and lands directly on `enriched_tracks`. Stages 5 and 6 must not call Beatport. Reason: `import-to-studio` already runs a long Node helper; an extra Beatport client + token-refresh path there is what caused the prior mid-run token-expiry incident.
- **import-to-studio writes only to DJ Studio's filesystem** — Stage 5a does NOT touch our DB. The pending-check is "does `beatport-sdk_<id>` exist in DJ Studio's `audio-library-table` AND have `mikKey` set". DJ Studio's own filesystem is the single source of truth. Within `_process_one`, the audio-library-table file is written LAST (after track-structures + 4 compressedAudioView binaries) so a Ctrl-C between writes leaves no skip-indicator on disk and the next run reprocesses cleanly.
- **JWT auto-refresh** — DJ Studio's access JWT lives ~60 min; long runs hit expiry. On a 401 from `cf.dj.studio`, `_process_one` re-decrypts `encryptedToken-v2.dat`, re-exchanges via `app-services.dj.studio`, and pushes the fresh JWT down to the running Node helper via the `setAccessJwt` command (defined in `dj_studio_sdk.js`). No helper restart, no model reload. Hard-abort only fires if the post-refresh retry ALSO 401s — at that point `encryptedToken-v2.dat` itself is invalid and only "open DJ Studio, sign in" can fix it.
- **repair-studio-library** — `dj detect repair-studio-library` finds half-baked entries (audio-library-table written but companions missing) and deletes them so `import-to-studio` reprocesses with the full pipeline. Three classifications: `recoverable` (in `enriched_tracks`, deleted by default), `orphan-free` (not in `enriched_tracks`, no mix references — skipped unless `--include-orphans`), `orphan-in-use` (referenced by a saved mix in `projects-table` — NEVER deleted, would leave a broken slot). Most half-baked entries come from DJ Studio's own light-analysis UI flows, not from our pipeline.
- **enrich-studio is the analysis-table creation point** — Stage 5b reads back from DJ Studio's library files and INSERT-or-UPDATEs `enriched_tracks_analysis` via `upsert_analysis(beatport_id, fields)` (stamps `dj_studio_at` automatically on insert).
- **mark_pipeline_done only handles rekordbox stamps** — `dj_studio_at` is set by `upsert_analysis`, not by the caller. Valid columns are `rekordbox_export_at` (Stage 6a) and `rekordbox_analysis_at` (Stage 6b). Passing anything else raises.
- **DJ Studio analysis is headless via SDK** — `detect/import_to_studio.py` decrypts DJ Studio's local refresh token (AES-256-CBC, hardcoded key in `encryptedToken-v2.dat`), exchanges it for a JWT via `app-services.dj.studio`, then drives `dj_studio_sdk.js` (a long-running Node helper that loads `@appmachine/beatport-sdk` + `@appmachine/ai-stems` + `@appmachine/ai-beatgrid` + the MIK WASM extractor and calls `cf.dj.studio/mixedinkey/analyze`). DJ Studio must be quit (port 61894 + `.beatport/` cache locks).
- **No phrase labels from DJ Studio** — DJ Studio's `track-structures-table.phraseData` is always empty (the renderer never calls the dormant ML phrase model). Real semantic phrase labels (Intro/Verse/Chorus/Outro/Up/Down/Bridge) come exclusively from Stage 6's rekordbox PSSI tag.
- **Rekordbox round-trip = three steps** — `export-to-rekordbox` pushes bare Beatport streaming entries (`FileType=20`) into a named playlist with no cue points (those would shadow rekordbox's own analysis output). Manual: open rekordbox → right-click playlist → Analyze Tracks. Then `import-rekordbox-analysis` reads PSSI + PCO2/PCOB into `rk_analysis_json`. Both stages JOIN `enriched_tracks_analysis` with `enriched_tracks` for the artist/title/key/bpm fields they need.
- **Beatport access token refresh** — `BEATPORT_ACCESS_TOKEN` ~10 min, `BEATPORT_SESSION_TOKEN` ~32 days. The session token auto-refreshes the access token on 401. Don't add manual refresh wrappers around individual stages. `connections/beatport.refresh_via_session(verbose=True)` (or `BEATPORT_DEBUG=1`) prints the real cause when refresh fails — usually means the persistent browser profile at `~/Music/dj-tools/state/browser-profile/` needs wiping so `--ui` can do a clean re-login.
- **Stage 1 (sync) cursor** — `--library` mode tracks the last `library_added_date` processed in the `cursors` table; re-runs only handle new Apple Music additions. `synced_tracks` keeps per-track outcome (`added` / `duplicate` / `fuzzy_miss` / `no_classify`) so a track is never reprocessed.
- **caffeinate on long-running commands** — `caffeinate.py` (top-level) provides a `caffeinate()` context manager that runs `caffeinate -i` to prevent macOS idle sleep. Applied to: `detect import-to-studio` (Node SDK analysis, can run hours), `detect enrich` (sequential Beatport API calls, can run 20+ min on large libraries), `detect radio-garden` (indefinite monitoring loop). The macOS power assertion is released automatically when the `caffeinate` process exits. Not needed for fast filesystem-only commands (`enrich-studio`, `import-rekordbox-analysis`, `export-to-rekordbox`).

### playlist (SQL → destination)

- **Stage 6a (`detect export-to-rekordbox`) and `playlist rekordbox` share the same core** — `playlist.to_rekordbox.push_to_rekordbox(rows, name)` does the writing. Stage 6a wraps it with the `rekordbox_export_at IS NULL` pending-query and an `on_added` callback to stamp the timestamp. `playlist rekordbox` calls the same function with no callback — pure ad-hoc curation, no pipeline-stamp side effects.
- **User SQL must return `beatport_id`** — `playlist.query.run_user_query` validates the query starts with `SELECT` (the only check; the column-shape error fires after fetch if `beatport_id` isn't in the result set). After exec, `fetch_full_rows` re-fetches via `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)` so push code always has artist/title/genre/key/bpm/length_ms regardless of how the user wrote their SQL. The query runs with the connection's full DB privileges — this tool assumes the user owns the database.
- **DJ Studio mix file format** — `playlist dj-studio` writes to `~/Music/DJ.Studio/Database/projects-table/<uuid>` with linear `mixList` and empty `autoEffects`. The minimal field set is `{key, name, genre, duration, trackCount, minBpm, maxBpm, createdAt, lastModified, mixList, autoEffects}` — all of which DJ Studio's reader (`djstudio/extractor.py`) consumes. If DJ Studio rejects a generated mix on a future version, the missing field will surface in DJ Studio's logs; add it here.
- **dj-studio destination requires tracks already imported** — checks `~/Music/DJ.Studio/Database/audio-library-table/*/beatport-sdk_<id>` for each beatport_id; missing ones are warned and skipped. Run `dj detect import-to-studio` first to populate the library.
