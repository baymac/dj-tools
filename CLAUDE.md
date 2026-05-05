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
  studio_sdk.py                 Shared SDK driver: SdkHelper + _shape_result + token decrypt
  dj_studio_sdk.js              Long-running Node helper for Stage 5
  studio_analyse.py             Stage 5: SDK analysis → enriched_tracks_analysis (DB only)
  export_to_rekordbox.py        Stage 6a: idempotent pending → rekordbox playlist
  import_rekordbox_analysis.py  Stage 6b: ingest PSSI + cues from ANLZ
  instagram.py / mixcloud.py / youtube.py / radio.py / podbean.py / reddit.py
                                Stage 2: per-platform Shazam capture
  shazam.py / parser.py         Audio recognition + tracklist parsing

sync/                           Stage 1: Apple Music → Beatport
  db.py / sync.py / classifier.py / cli.py

playlist/                       SQL-curated push to Beatport or rekordbox
  query.py                      Run user SQL → list[beatport_id] + full-row fetch
  to_beatport.py                Push to a Beatport playlist
  to_rekordbox.py               Push to a rekordbox playlist (also called by Stage 6a)
  cli.py

djstudio/                       Read DJ Studio's local files (used for ad-hoc inspection)
  extractor.py                  audio-library-table + projects-table reader
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
uv run dj_cli.py detect studio-analyse                                                  # Stage 5: SDK → enriched_tracks_analysis
uv run dj_cli.py detect studio-analyse --ids 12345,67890 --force --verbose              #   re-process specific tracks (debugging)
uv run dj_cli.py detect export-to-rekordbox
uv run dj_cli.py detect import-rekordbox-analysis

# SQL → playlist (Beatport or rekordbox)
uv run dj_cli.py playlist beatport  --query "SELECT beatport_id FROM enriched_tracks WHERE ..." --name "..."
uv run dj_cli.py playlist rekordbox --query "..." --name "..."

# Maintenance
uv run helpers/cleanup_playlist.py "Playlist Name" --dry-run
```

## Key Design Decisions

### Enrichment pipeline (detect + sync)

- **Two enriched tables, no mirror** — `enriched_tracks` carries everything Beatport-derived (basic search-result fields + the 6 catalog-detail extras). `enriched_tracks_analysis` is sparse: keyed on `beatport_id`, holds DJ Studio analysis (`mik_key`, `mik_nrg`, per-stem `*_avg`/`*_peak`, `analysis_json` with full energy segments + 1Hz stem curves + per-segment stem RMS) + rekordbox PSSI (`rk_analysis_json`) + per-stage timestamps. A row exists in the analysis table only after `studio-analyse` has populated it. Joins (e.g., `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)`) build the full picture at query time.
- **Beatport metadata in one place** — Full track-detail (label, ISRC, mix_name, sub_genre, length_ms, catalog_number) is fetched **only** in `detect/enrich.py` (and inline-extracted from playlist responses by `detect/sync_beatport.py`) and lands directly on `enriched_tracks`. Stages 5 and 6 must not call Beatport. Reason: `studio-analyse` already runs a long Node helper; an extra Beatport client + token-refresh path there is what caused the prior mid-run token-expiry incident.
- **studio-analyse writes only to our DB** — Stage 5 calls `upsert_analysis(beatport_id, fields)` to populate `enriched_tracks_analysis` (stamps `dj_studio_at` on insert). DJ Studio's filesystem is never touched. The skip-rule for re-runs is "row exists in `enriched_tracks_analysis` for this beatport_id" (override with `--force`); for ad-hoc reruns of specific tracks pass `--ids ID,ID,...`. SDK output was previously verified byte-for-byte against DJ Studio's stored values: mikKey/mikEnergy/BPM/duration/beat-count/energy segments all match exactly. The two divergences DJ Studio applies (rounded BPM, segment merging, cue-point trimming, BP-key override of mikKey for certain tracks) are display-time post-processing — we keep the fuller raw signal.
- **JWT auto-refresh** — DJ Studio's access JWT lives ~60 min; long runs hit expiry. On a 401 from `cf.dj.studio`, `studio-analyse` re-decrypts `encryptedToken-v2.dat`, re-exchanges via `app-services.dj.studio`, and pushes the fresh JWT down to the running Node helper via the `setAccessJwt` command (defined in `dj_studio_sdk.js`). No helper restart, no model reload. Hard-abort only fires if the post-refresh retry ALSO 401s — at that point `encryptedToken-v2.dat` itself is invalid and only "open DJ Studio, sign in" can fix it.
- **mark_pipeline_done only handles rekordbox stamps** — `dj_studio_at` is set by `upsert_analysis`, not by the caller. Valid columns are `rekordbox_export_at` (Stage 6a) and `rekordbox_analysis_at` (Stage 6b). Passing anything else raises.
- **DJ Studio analysis is headless via SDK** — `detect/studio_sdk.py` decrypts DJ Studio's local refresh token (AES-256-CBC, hardcoded key in `encryptedToken-v2.dat`), exchanges it for a JWT via `app-services.dj.studio`, then drives `dj_studio_sdk.js` (a long-running Node helper that loads `@appmachine/beatport-sdk` + `@appmachine/ai-stems` (Demucs) + `@appmachine/ai-beatgrid` + the MIK WASM extractor and calls `cf.dj.studio/mixedinkey/analyze`). The Demucs model weights live at `~/Library/Application Support/DJ.Studio/extensions/djs-stems/models/htdemucs_fast_encrypted.pt` — installed by DJ Studio itself, shared by us. DJ Studio must be quit (port 61894 + `.beatport/` cache locks).
- **Stem curves + per-segment RMS** — the Node helper computes per-1024-sample-bucket RMS (~23ms resolution) per stem when running Demucs and ships them back as base64 uint16. `_shape_result` decodes them into (a) `stems[stem].curve_1hz` — one mean RMS per second of audio, ~300 floats per stem for a 5-min track, used for "where does X come in?" queries — and (b) `stems[stem].per_segment` — index-aligned with `energy.segments[]`, for "vocals during the chorus" queries. Both live in `analysis_json`.
- **No phrase labels from DJ Studio** — DJ Studio's `track-structures-table.phraseData` is always empty (the renderer never calls the dormant ML phrase model). Real semantic phrase labels (Intro/Verse/Chorus/Outro/Up/Down/Bridge) come exclusively from Stage 6's rekordbox PSSI tag.
- **Rekordbox round-trip = three steps** — `export-to-rekordbox` pushes bare Beatport streaming entries (`FileType=20`) into a named playlist with no cue points (those would shadow rekordbox's own analysis output). Manual: open rekordbox → right-click playlist → Analyze Tracks. Then `import-rekordbox-analysis` reads PSSI + PCO2/PCOB into `rk_analysis_json`. Both stages JOIN `enriched_tracks_analysis` with `enriched_tracks` for the artist/title/key/bpm fields they need.
- **Beatport access token refresh** — `BEATPORT_ACCESS_TOKEN` ~10 min, `BEATPORT_SESSION_TOKEN` ~32 days. The session token auto-refreshes the access token on 401. Don't add manual refresh wrappers around individual stages. `connections/beatport.refresh_via_session(verbose=True)` (or `BEATPORT_DEBUG=1`) prints the real cause when refresh fails — usually means the persistent browser profile at `~/Music/dj-tools/state/browser-profile/` needs wiping so `--ui` can do a clean re-login.
- **Stage 1 (sync) cursor** — `--library` mode tracks the last `library_added_date` processed in the `cursors` table; re-runs only handle new Apple Music additions. `synced_tracks` keeps per-track outcome (`added` / `duplicate` / `fuzzy_miss` / `no_classify`) so a track is never reprocessed.
- **caffeinate on long-running commands** — `caffeinate.py` (top-level) provides a `caffeinate()` context manager that runs `caffeinate -i` to prevent macOS idle sleep. Applied to: `detect studio-analyse` (Node SDK analysis, ~23s/track, can run hours over a full library), `detect enrich` (sequential Beatport API calls, can run 20+ min on large libraries), `detect radio-garden` (indefinite monitoring loop). The macOS power assertion is released automatically when the `caffeinate` process exits. Not needed for fast filesystem-only commands (`import-rekordbox-analysis`, `export-to-rekordbox`).

### playlist (SQL → destination)

- **Stage 6a (`detect export-to-rekordbox`) and `playlist rekordbox` share the same core** — `playlist.to_rekordbox.push_to_rekordbox(rows, name)` does the writing. Stage 6a wraps it with the `rekordbox_export_at IS NULL` pending-query and an `on_added` callback to stamp the timestamp. `playlist rekordbox` calls the same function with no callback — pure ad-hoc curation, no pipeline-stamp side effects.
- **User SQL must return `beatport_id`** — `playlist.query.run_user_query` validates the query starts with `SELECT` (the only check; the column-shape error fires after fetch if `beatport_id` isn't in the result set). After exec, `fetch_full_rows` re-fetches via `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)` so push code always has artist/title/genre/key/bpm/length_ms regardless of how the user wrote their SQL. The query runs with the connection's full DB privileges — this tool assumes the user owns the database.
- **No DJ Studio writes from this tool** — the previous `playlist dj-studio` destination wrote `projects-table/<uuid>` + `projects-meta-table/<uuid>` files, but DJ Studio also tracks per-mix UI state in IndexedDB (`~/Library/Application Support/DJ.Studio/IndexedDB/local-web_*.indexeddb.leveldb/`) that we couldn't write to — meaning UI delete was a no-op for tool-created mixes (the right-click → Delete flow looks up the IndexedDB row, doesn't find it, silently fails). We removed the destination rather than ship a half-working write path. DJ Studio is now read-only for this tool: we drive its SDK for analysis (`studio-analyse`) and read its library + projects-table for inspection only.
