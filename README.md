# dj

Unified DJ toolkit. Builds a fully-analysed track library: pull tracks in from Apple Music, Beatport, and detected audio sources, progressively enrich each track with Beatport metadata, DJ Studio analysis (key/energy/cues/stems), and rekordbox phrase tags. Then push any SQL-curated subset to a Beatport playlist, a rekordbox playlist, or a DJ Studio mix.

All tool-generated files live under `~/Music/dj-tools/`:

```
~/Music/dj-tools/
├── dj.db                              SQLite — all tables
├── logs/<command>/YYYY-MM-DD_<id>.log per-command log files (one per run)
├── state/                             session files, config, Beatport browser profile
├── cache/musickit/                    Swift bridge build cache
├── exports/                           default for export-* helpers
└── backups/
    ├── apple-music/                   default for backup_apple_music helper
    └── rekordbox/                     master.db pre-write backups
```


---

## Setup

```bash
uv sync
uv run playwright install chromium   # needed for Beatport browser login
```

Copy `.env.example` to `.env` and fill in credentials before using `detect` or `sync`.

Rekordbox must be **closed** before any rekordbox write (`detect export-to-rekordbox`, `detect import-rekordbox-analysis`, `playlist rekordbox`).

DJ Studio must be **closed** before `detect studio-analyse`.

---

## Pipeline at a glance

```
                                  Apple Music library / playlists
                                              │
                                              │  Stage 1: dj sync music-beatport
                                              ↓
                                      Beatport playlists ────┐
                                                             │
   Audio sources                                             │  Stage 4: dj detect sync-beatport
   (Instagram, YouTube, Mixcloud, SoundCloud,                │
    Radio Garden, Podbean, Reddit, topdjmixes)               │
        │                                                    │
        │  Stage 2: dj detect <platform>                     │
        ↓                                                    │
   detected_tracks                                           │
        │                                                    │
        │  Stage 3: dj detect enrich                         │
        ↓                                                    │
   enriched_tracks  ←─────────────────────────────────────────┘
   (basic Beatport fields + label/ISRC/mix_name/sub_genre/length_ms)
                              │
                              │  Stage 5: dj detect studio-analyse
                              │           (drives DJ Studio's bundled SDK headlessly,
                              │            writes directly to our DB — no DJ Studio writes)
                              ↓
   enriched_tracks_analysis  ←  mik_key, mik_nrg, per-stem RMS,
                                 analysis_json (full energy segments + 1Hz stem curves
                                 + per-segment stem RMS)  +  dj_studio_at
                              │
                              │  Stage 6a: dj detect export-to-rekordbox
                              │            [open rekordbox → Analyze Tracks → close]
                              │  Stage 6b: dj detect import-rekordbox-analysis
                              ↓
   enriched_tracks_analysis  +  rk_analysis_json (PSSI phrases + cues)
                              +  rekordbox_export_at, rekordbox_analysis_at
```

Each enrichment stage is idempotent. `enriched_tracks_analysis` carries per-stage timestamps (`dj_studio_at`, `rekordbox_export_at`, `rekordbox_analysis_at`); re-runs only pick up new work. You can stop at any stage — every stage is independently useful. A row exists in `enriched_tracks_analysis` only after `studio-analyse` has populated it; `enriched_tracks` carries everything Beatport-derived without any sibling rows in the analysis table.

---

## Command tree

```
dj
├── login-beatport [--ui | --cookie]                   Refresh Beatport tokens

├── sync                                               Stage 1: Apple Music → Beatport
│   └── music-beatport
│       ├── check-connections
│       ├── list-playlists
│       └── sync                                       [--playlist NAME] [--library] [--favorites]
│                                                      [--library-and-favorites] [--all]
│                                                      [--dry-run] [--limit N] [--verbose] [--threshold F]
│
├── detect                                             Stage 2: detect tracks via Shazam
│   ├── instagram <url>                                [--username] [--password] [--output] [--json]
│   ├── radio-garden <url>                             [--interval N] [--capture N] [--duration N]
│   ├── mixcloud <url>                                 [--username] [--password] [--interval N]
│   ├── youtube <url>                                  [--interval N] [--capture N] [--output] [--json]
│   ├── soundcloud <url>                               [--interval N] [--capture N] [--output] [--json]
│   ├── podbean <url>                                  [--interval N] [--capture N] [--output] [--json]
│   ├── reddit <url>                                   (paste-into-vi tracklist parser)
│   ├── topdjmixes <url>                               (paste-into-vi tracklist parser)
│   │
│   ├── gems                                            Discover low-play tracks by genre + recency
│   │                                                   [--source S] [--genre G] [--count N] [--date D] [--no-save]
│   │
│   ├── history / sessions / *-history                 Inspect detection state
│   ├── *-delete-session <id>                          Remove a scan session
│   ├── fix-session <id>                               Correct detected tracks using a confirmed tracklist (stdin)
│   │                                                  [--threshold F] [--apply]
│   ├── login-instagram / login-mixcloud               Save credentials
│   │
│   ├── enrich                                         Stage 3: detected → Beatport metadata
│   │                                                  [--dry-run] [--limit N] [--verbose] [--threshold F] [--retry-misses]
│   ├── sync-beatport                                  Stage 4: Beatport library → enriched_tracks
│   │                                                  [--dry-run] [--limit N] [--verbose]
│   ├── studio-analyse                                 Stage 5: drive DJ Studio's SDK → enriched_tracks_analysis
│   │                                                  [--ids ID,...] [--limit N] [--verbose] [--force] [--retry-failed]
│   ├── export-to-rekordbox                            Stage 6a: push to rekordbox playlist
│   │                                                  [--playlist NAME] [--limit N] [--dry-run] [--force]
│   ├── import-rekordbox-analysis                      Stage 6b: ingest rekordbox PSSI + cues
│   │                                                  [--limit N] [--force] [--verbose]
│   │
│   ├── enriched [-n N]                                List enriched tracks
│   ├── enrich-runs [-n N]                             Past enrich run summaries
│   └── enrich-tracks <type> <id> [--misses]           Per-session enrichment status
│
└── playlist                                           Push a SQL-curated subset to a destination
    ├── beatport --query SQL --name NAME               Beatport playlist
    └── rekordbox --query SQL --name NAME              Rekordbox playlist
```

---

## login-beatport

Stages 1, 3, and 4 talk to Beatport. They need `BEATPORT_ACCESS_TOKEN` and `BEATPORT_SESSION_TOKEN` in `.env`. Run this once to bootstrap; after that the token auto-refreshes.

```bash
uv run dj_cli.py login-beatport          # auto: tries session cookie, then browser
uv run dj_cli.py login-beatport --ui     # open a visible browser window to log in
uv run dj_cli.py login-beatport --cookie # refresh via BEATPORT_SESSION_TOKEN only
```

**How `--ui` works:** opens a real browser window (Brave/Chrome if installed, else Chromium) with a persistent profile at `~/.playlist-syncer/browser-profile`. If you're already logged in, the token is grabbed and the window closes. Otherwise log in and it closes once the session is detected.

**Token lifetime:** `BEATPORT_ACCESS_TOKEN` expires in ~10 min. `BEATPORT_SESSION_TOKEN` lasts ~32 days. As long as the session token is valid, all stages auto-refresh the access token.

---

# Stage 1 — sync Apple Music → Beatport playlists

Pushes Apple Music tracks (library, favourites, or any named playlist) into matching Beatport genre playlists you own. Each Apple Music track is fuzzy-matched against Beatport search results, classified by genre, and added to the right destination playlist. Per-track outcomes (`added`, `duplicate`, `fuzzy_miss`, `no_classify`) are written to `synced_tracks` so a track is never reprocessed. Interrupted runs resume cleanly.

Log written to `~/Music/dj-tools/logs/sync-music-beatport/YYYY-MM-DD_<run_id>.log`.

```bash
uv run dj_cli.py sync music-beatport check-connections   # verify Apple Music + Beatport auth
uv run dj_cli.py sync music-beatport list-playlists      # show your Beatport playlists

# Pick one source per run
uv run dj_cli.py sync music-beatport sync --library                # library songs (incremental via cursor)
uv run dj_cli.py sync music-beatport sync --favorites              # Favourite Songs playlist
uv run dj_cli.py sync music-beatport sync --library-and-favorites  # union of both
uv run dj_cli.py sync music-beatport sync --all                    # all songs, no filter
uv run dj_cli.py sync music-beatport sync --playlist "Ibiza 2026"  # named Apple Music playlist

# Common flags
uv run dj_cli.py sync music-beatport sync --library --dry-run
uv run dj_cli.py sync music-beatport sync --library --limit 100
uv run dj_cli.py sync music-beatport sync --library --verbose
uv run dj_cli.py sync music-beatport sync --library --threshold 0.85
```

The `--library` mode tracks where it left off via the `cursors` table (last `library_added_date` processed) so re-runs only handle new Apple Music additions.

---

# Stage 2 — detect tracks from audio sources

Identifies tracks playing in Instagram posts, radio streams, Mixcloud mixes, YouTube videos, SoundCloud mixes, and Podbean episodes via Shazam, or extracts them from Reddit / topdjmixes text posts via a paste-into-vi parser. Results land in `detected_tracks` (one row per unique track, deduped by Shazam key or artist + title). Re-scanning the same URL never creates duplicates.

Mixcloud, YouTube, SoundCloud, and Podbean scans auto-resume from where they left off if interrupted.

```bash
uv run dj_cli.py detect instagram https://www.instagram.com/p/XXXXX/

uv run dj_cli.py detect radio-garden https://radio.garden/listen/station-name
uv run dj_cli.py detect radio-garden <url> --interval 60    # check every 60s
uv run dj_cli.py detect radio-garden <url> --duration 120   # run for 2 hours

uv run dj_cli.py detect mixcloud https://www.mixcloud.com/djname/mixname/
uv run dj_cli.py detect youtube https://www.youtube.com/watch?v=XXXX
uv run dj_cli.py detect soundcloud https://soundcloud.com/dj/mix-name        # share-link tracking params auto-stripped
uv run dj_cli.py detect podbean https://www.podbean.com/ew/pb-XXXX
uv run dj_cli.py detect reddit https://www.reddit.com/r/HypeTracks/comments/XXXXX/post_title/
uv run dj_cli.py detect topdjmixes https://www.topdjmixes.com/some-mix-page/
```

**Credentials:**
- Instagram: `IG_USERNAME` / `IG_PASSWORD` in `.env`, or `dj detect login-instagram`.
- Mixcloud: `MC_USERNAME` / `MC_PASSWORD`, or `dj detect login-mixcloud`.
- SoundCloud: optional OAuth via `SOUNDCLOUD_CLIENT_ID` / `SOUNDCLOUD_CLIENT_SECRET` (register an app at https://soundcloud.com/you/apps). When configured, set/track metadata comes from SoundCloud's official API — clean artist/title fields, no rate-limit pain. When absent, falls back to yt-dlp scrape + URL-slug derivation (works but lower fidelity). Share-link tracking params (`?si=…`, `&utm_*=…`) are stripped automatically. The handler auto-detects three URL shapes:
    - **Set** (`/<user>/sets/<slug>`) → enumerate child tracks via metadata, no audio download.
    - **Single track ≤15 min** → save the track's metadata as one row (no Shazam scan).
    - **Single track >15 min** (radio episodes, DJ mixes) → Shazam-by-chunks audio scan.
    - **Personalized `/discover/` URLs** (e.g. `personalized-tracks::<user>:<id>`) → require user-bound OAuth (run `dj detect login-soundcloud` once; opens browser, OAuth dance, saves a refresh token). After login the handler auto-uses the user token for all calls; without it `/discover/` URLs return a clear "login required" message. Make sure your SoundCloud app has `http://localhost:8080/callback` (or your custom `SOUNDCLOUD_REDIRECT_URI`) in its Redirect URI list.
- YouTube: no credentials needed. yt-dlp extracts cookies from the first available browser (Brave → Chrome → Safari → Firefox) and caches them for one week. If YouTube returns a bot-detection challenge, the cache is discarded and cookies are re-extracted before retrying. If no browser is available the fallback passes `--cookies-from-browser chrome` live.
- Reddit: none. Public JSON API. Works on any subreddit text post whose body contains `Artist - Title` lines (markdown links and `[brackets]` are stripped).
- topdjmixes: none. Paste-into-vi flow (same parser shape as Reddit). Works on any tracklist with `01. Artist – Title` lines — leading position numbers and `[label]` brackets are stripped.

### History and sessions

```bash
uv run dj_cli.py detect history             # all detected tracks, newest first
uv run dj_cli.py detect history -n 100

uv run dj_cli.py detect sessions youtube       # session list with track counts
uv run dj_cli.py detect sessions mixcloud
uv run dj_cli.py detect sessions soundcloud
uv run dj_cli.py detect sessions radio
uv run dj_cli.py detect sessions instagram
uv run dj_cli.py detect sessions podbean
uv run dj_cli.py detect sessions reddit
uv run dj_cli.py detect sessions topdjmixes

uv run dj_cli.py detect sessions podbean 24    # detected_tracks for one session, in a table
uv run dj_cli.py detect sessions youtube 7     # (Pos, Artist, Title, Apple Music URL, enrich_outcome)

uv run dj_cli.py detect instagram-history           # grouped by post
uv run dj_cli.py detect instagram-history --tracks  # flat track list only
uv run dj_cli.py detect radio-history
uv run dj_cli.py detect mixcloud-history
uv run dj_cli.py detect youtube-history
uv run dj_cli.py detect soundcloud-history
uv run dj_cli.py detect podbean-history
uv run dj_cli.py detect reddit-history
uv run dj_cli.py detect topdjmixes-history

uv run dj_cli.py detect mixcloud-delete-session <id>
uv run dj_cli.py detect youtube-delete-session <id>
uv run dj_cli.py detect soundcloud-delete-session <id>
uv run dj_cli.py detect podbean-delete-session <id>
uv run dj_cli.py detect reddit-delete-session <id>
uv run dj_cli.py detect topdjmixes-delete-session <id>
```

### Correcting a session's detected tracks — `fix-session`

Shazam occasionally mis-identifies tracks. `fix-session` lets you paste a confirmed tracklist (from a set description, the DJ's own post, etc.) and remove any detected track that can't be matched to it.

```bash
# Dry-run — shows what would be removed (default):
uv run dj_cli.py detect fix-session 7

# Apply the removals:
uv run dj_cli.py detect fix-session 7 --apply

# Lower the match bar (default 0.75):
uv run dj_cli.py detect fix-session 7 --apply --threshold 0.6
```

Paste the confirmed tracklist into stdin (press Ctrl-D when done). Lines that match a detected track above `--threshold` are kept; the rest are removed from the session and deleted from `detected_tracks` if they haven't been enriched and aren't shared with another session. `--apply` is required to actually delete — without it the command prints the diff and exits.

---

# Discover hidden gems — `detect gems`

`detect gems` surfaces low-play / under-the-radar tracks in a genre, released within a chosen time window, across four platforms. It does **not** save finds automatically — instead it opens an interactive review where you listen to each track and decide. Approved tracks land in `detected_tracks` and flow straight into `detect enrich` and the rest of the pipeline.

Run it fully interactive (prompts for every choice) or pass flags — any omitted flag is prompted for:

```bash
uv run dj_cli.py detect gems                                                          # fully interactive
uv run dj_cli.py detect gems --source beatport --genre "Tech House" --count 10 --date 1mo
uv run dj_cli.py detect gems --source soundcloud --count 15 --date 6mo
uv run dj_cli.py detect gems --source bandcamp --count 5 --date 6mo --no-save          # show only, skip review
```

| Flag | Values | Description |
|---|---|---|
| `--source` | `spotify` / `soundcloud` / `bandcamp` / `beatport` | Platform to search |
| `--genre` | `Tech House` | Genre (only Tech House is mapped today) |
| `--count` / `-n` | 1–20 | Number of **new** tracks to return |
| `--date` | `1mo` / `6mo` / `1yr` / `3yr` | Max track age (release window) |
| `--no-save` | — | Show the results table and skip the review step entirely (testing) |

**Per-source "gem" signal** — each platform exposes different data, so the genre filter and the low-play proxy differ:

| Source | Genre filter | Low-play proxy | Notes |
|---|---|---|---|
| **Beatport** | exact `genre_id` (real taxonomy) | excludes Hype (label-paid promotion) tracks | most genre-accurate; result table shows BPM + Camelot key; Beatport has no public play count |
| **SoundCloud** | `genres=` tag search | `playback_count < 5000` | real play counts via the public API |
| **Spotify** | editorial-playlist mining | `popularity ≤ 25` (widens to 35 if sparse) | Spotify's `genre:` search filter is unreliable for sub-genres, so it mines genre playlists for low-popularity tracks |
| **Bandcamp** | `tag_norm_names` via `discover/1/discover_web` | none — Bandcamp exposes no play count | tags are uploader-applied free text, so genre accuracy is approximate |

For strict genre accuracy, prefer **Beatport** — it is the only source with an authoritative genre taxonomy. Bandcamp tags in particular are uploader-supplied and noisy.

**Review.** After a scan, gems are not saved — `detect gems` walks the finds one at a time, printing each track's link so you can open it and listen, then prompts for a decision:

- **approve** (`a`) — the track is saved to `detected_tracks` and enters the pipeline.
- **reject** (`r`) — the track is recorded in `rejected_gems` and never enters the pipeline; it won't surface again in future scans.
- **skip** (`s`, the default) — the track is left undecided and not persisted anywhere, so it can reappear in a later scan.
- **quit** (`q`) — stop reviewing; the remaining tracks are left undecided.

**Persistence and cross-run dedup.** Approving at least one track records a `sessions` row (`type='gems'`) plus a `gem_scans` row (source, genre, requested/found counts, date window) and one `gem_tracks` row per approved track (url, release date, plays/popularity); approved tracks land in `detected_tracks` through the normal dedup path. Rejected tracks go to `rejected_gems` instead. The **next** run on the same platform skips every track it already approved *or* rejected and keeps paging until it has `--count` genuinely-new tracks — there is no fixed page offset, so it works even as a platform's results reshuffle over time. Prior gems (approved or rejected) whose release date is older than the current `--date` window are "faded" out of the comparison set (they cannot recur in a narrower window anyway), keeping the dedup check cheap. `--no-save` skips review and all persistence — results display but nothing is written, and the dedup history is left untouched.

**Credentials:**
- Spotify: `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` in `.env` (create an app at https://developer.spotify.com/dashboard). Prompted for interactively if missing.
- SoundCloud: `SOUNDCLOUD_CLIENT_ID` / `SOUNDCLOUD_CLIENT_SECRET` — the same credentials Stage 2 uses.
- Beatport: the usual `BEATPORT_ACCESS_TOKEN` / `BEATPORT_SESSION_TOKEN` (same as Stages 1, 3, 4).
- Bandcamp: none — public discover API.

---

# Stage 3 — enrich detected tracks with Beatport metadata

Takes everything in `detected_tracks` that doesn't have a Beatport match yet, fuzzy-matches each one against Beatport search, and pulls full track metadata. Tracks with no result or score below threshold are marked on `detected_tracks.enrich_outcome` (`not_found` or `fuzzy_miss`) and skipped on future runs.

Each match writes one row to `enriched_tracks` carrying every Beatport-derived field on the same row: the basic search-result fields (`bpm`, `key`, `genre`, `release_date`, `beatport_id`, `beatport_link`, `artist`, `title`, `apple_music_url`) plus the catalog-detail extras (`mix_name`, `label`, `catalog_number`, `isrc`, `sub_genre`, `length_ms`) fetched from `/v4/catalog/tracks/{id}/`.

Beatport-sourced data is fetched **only** here (and inline-extracted by Stage 4 from the playlist response). Stages 5 and 6 do not call Beatport.

```bash
uv run dj_cli.py detect enrich                       # enrich all pending tracks
uv run dj_cli.py detect enrich --dry-run
uv run dj_cli.py detect enrich --limit 50
uv run dj_cli.py detect enrich --verbose             # print per-track Beatport detail
uv run dj_cli.py detect enrich --threshold 0.8       # stricter match (default: 0.72)
uv run dj_cli.py detect enrich --retry-misses        # retry previously missed tracks
```

Log written to `~/Music/dj-tools/logs/enrich/YYYY-MM-DD_<run_id>.log`. Every other stage writes to `~/Music/dj-tools/logs/<stage>/YYYY-MM-DD_<HHMMSS>.log` automatically.

---

# Stage 4 — pull Beatport library tracks directly

For tracks already in your Beatport library (bought, favourited, in playlists), there is no detection step — just sync them straight into `enriched_tracks`. The catalog-detail extras (`mix_name`/`label`/`isrc`/`sub_genre`/`length_ms`) are pulled inline from the same playlist response so no extra HTTP call per track is needed.

```bash
uv run dj_cli.py detect sync-beatport
uv run dj_cli.py detect sync-beatport --dry-run
uv run dj_cli.py detect sync-beatport --limit 100
uv run dj_cli.py detect sync-beatport --verbose
```

Stages 3 and 4 produce identical-shaped rows in `enriched_tracks`. Stages 5 and 6 don't care which path a row came from.

---

# Stage 5 — DJ Studio analysis (key, energy, cues, beatgrid, stems)

One command: **`studio-analyse`** drives DJ Studio's bundled SDK headlessly and writes results directly into `enriched_tracks_analysis`. **Doesn't touch DJ Studio's filesystem at all** — DJ Studio's `audio-library-table` / `track-structures-table` / `compressedAudioView*` binaries are never written to.

### studio-analyse — drive DJ Studio's analysis headlessly

Uses your DJ Studio account + the bundled SDK to fetch full Beatport tracks, run the same MIK + ai-beatgrid + ai-stems pipeline DJ Studio uses internally, and write rows directly into `enriched_tracks_analysis` — no UI interaction, no DJ Studio filesystem pollution.

**Per track captured (in our DB):**

| Source | Output |
|---|---|
| `cf.dj.studio/mixedinkey/analyze` (via WASM features) | `mik_key`, `mik_key_secondary`, `mik_key_confidence`, `mik_nrg` (1-10), full energy segments + cue points (in `analysis_json`) |
| `@appmachine/ai-beatgrid` (TorchScript) | `tempo_precise` (full-precision BPM), all beat positions, downbeat (in `analysis_json`) |
| `@appmachine/ai-stems` Demucs Fast | per-stem `*_avg` / `*_peak` RMS floats; in `analysis_json`, `stems[stem].curve_1hz` (1Hz time-series) + `stems[stem].per_segment` (avg/peak per energy segment) |

(Beatport metadata — mix_name, label, catalog_number, ISRC, sub_genre, length_ms — was already fetched by Stage 3 and is on `enriched_tracks`.)

**Prerequisites:**
1. **Quit DJ Studio (Cmd+Q)** before running. Its SDK conflicts with ours on port 61894 + `.beatport/` cache locks. Pre-flight check aborts with a clear message if DJ Studio is running.
2. Sign into DJ Studio + Beatport via the UI at least once. Demucs model weights (`~/Library/Application Support/DJ.Studio/extensions/djs-stems/models/htdemucs_fast_encrypted.pt`) must be downloaded — DJ Studio prompts on first launch. We share these weights; we don't run our own Demucs.
3. DJ Studio refresh token must be valid. If expired, open DJ Studio briefly to refresh, quit it, re-run.

**`cf.dj.studio`** is DJ Studio's Cloudflare-hosted classification API. The local WASM extracts pitch/energy features; the server classifies them into a Camelot key + 1-10 energy + segment boundaries + cue points. Same flow the desktop app uses internally — verified bit-identical output for `mik_key`/`mik_nrg`/`bpm`/`duration`/`beat_count` against tracks DJ Studio analysed via its UI. (DJ Studio applies some display-time post-processing — rounded BPM, segment merging, cue trimming, BP-key override of mikKey — that we deliberately skip to keep the fuller raw signal.)

This command runs `caffeinate -i` automatically — your Mac won't sleep mid-run. Same applies to `detect enrich` (sequential Beatport API calls) and `detect radio-garden` (indefinite monitoring loop).

```bash
# Small sanity-check batch
uv run dj_cli.py detect studio-analyse --limit 5 --verbose

# Full batch
uv run dj_cli.py detect studio-analyse --verbose

# Re-process specific tracks (e.g. after fixing a bug in _shape_result)
uv run dj_cli.py detect studio-analyse --ids 23330162,21531599 --force --verbose
```

**Flags:**
- `--ids ID,ID,...`: only analyse these beatport IDs. Bypasses `--limit` and the short-track / failure-sidecar filters; still respects the dedupe filter unless paired with `--force`.
- `--limit N`: stop after N tracks (0 = no limit). Ignored when `--ids` is set.
- `--force`: re-process tracks even if a row already exists in `enriched_tracks_analysis`.
- `--retry-failed`: ignore the hard-failure sidecar and re-attempt tracks that previously hit the failure cap.

**Idempotent:** skip rule is "row exists in `enriched_tracks_analysis` for this beatport_id". Re-runs only process new tracks.

**JWT auto-refresh mid-run:** DJ Studio's access JWT lasts ~60 min. On the first 401 from `cf.dj.studio` the run re-decrypts `encryptedToken-v2.dat`, re-exchanges via `app-services.dj.studio`, pushes the fresh token down to the running Node helper (`setAccessJwt` command — no helper restart, no model reload), and retries the failed track. Long batches don't need babysitting. If the post-refresh retry also 401s, the run aborts with a clear message — that means `encryptedToken-v2.dat` itself is invalid (open DJ Studio, sign in, quit, re-run).

**Failure handling:** transient `cf.dj.studio` failures are auto-retried inside the Node helper (4 attempts, exponential backoff up to 9s). Tracks that still fail get a second pass at the end of the batch after a 5s pause. Tracks that fail on both first pass and retry are recorded in a sidecar (`~/Music/dj-tools/state/studio_analyse_failures.json`) and auto-skipped on subsequent runs once they hit `MAX_FAILURE_ATTEMPTS` (3) — bypass with `--retry-failed`. The summary distinguishes "written / recovered on retry / permanently failed" with per-track error reasons.

**Per-track timing:** ~30-50s per track on first run (SDK + model cold-start), ~25-30s steady-state. ~2GB peak memory (Demucs models). 100 tracks ≈ 50-60 minutes.

### Stored in `enriched_tracks_analysis` after Stage 5

```
beatport_id              -- PRIMARY KEY (link to enriched_tracks via JOIN)
mik_key, mik_nrg         -- from cf.dj.studio classifier
mik_key_secondary        -- secondary key candidate
mik_key_confidence       -- 0-1 confidence on main key
tempo_precise            -- full-precision BPM (DJ Studio rounds; we don't)
duration_sec             -- track duration
cue_points_count         -- count from classifier
vocals_avg, drums_avg, bass_avg, melody_avg     -- per-stem aggregate RMS
vocals_peak, drums_peak, bass_peak, melody_peak -- per-stem peak RMS
analysis_json            -- compact JSON blob: full energy segments,
                            cue points, tempo, structure, stems with
                            curve_1hz (~300 floats per stem) +
                            per_segment (avg/peak per energy segment)
dj_studio_at             -- set on first INSERT
```

**Not stored** (intentionally): semantic phrase labels (intro/chorus/breakdown/etc.). DJ Studio doesn't produce those — its renderer never calls the dormant ML phrase model and real `track-structures-table.phraseData` arrays are empty. For real labelled phrases use Stage 6.

---

# Stage 6 — rekordbox phrase analysis

Rekordbox's automatic Analyze produces semantic phrase labels (Intro / Verse / Chorus / Outro / Up / Down / Bridge) via its proprietary PSSI tag, plus auto-placed memory cues and hot cues. Two commands plus one manual step:

1. **`export-to-rekordbox`** pushes tracks into a rekordbox playlist as Beatport streaming entries.
2. Manually open rekordbox → playlist → right-click → **Analyze Tracks**.
3. **`import-rekordbox-analysis`** reads the resulting ANLZ files (PSSI + cues) into `enriched_tracks_analysis.rk_analysis_json`.

### export-to-rekordbox — push tracks into a rekordbox playlist

Adds tracks to your rekordbox library as Beatport streaming entries (`FileType=20`, the same kind rekordbox creates when you drag a Beatport track from its in-app browser) and to a named playlist. **Doesn't push cue points** — those would shadow whatever rekordbox computes. Tracks land bare; rekordbox fills in beat grid + cue points + phrase tags itself.

**Prerequisite:** rekordbox must be quit (locks `master.db`). Pre-flight check aborts if it's running. `master.db` is backed up to `<rekordbox-share>/claude-backups/` before any write.

**Idempotent:** skip rule is `rekordbox_export_at IS NULL`. Re-runs pick up only new tracks. `--force` overrides.

```bash
uv run dj_cli.py detect export-to-rekordbox --limit 5 --dry-run
uv run dj_cli.py detect export-to-rekordbox --playlist "DJ Tools - Enrich"
```

### Manual: Analyze Tracks in rekordbox

Open rekordbox, find the playlist, select all tracks, right-click → **Analyze Tracks**. This writes ANLZ files containing PSSI phrase tags + auto-placed cues. Quit rekordbox once analysis finishes.

### import-rekordbox-analysis — read ANLZ data into rk_analysis_json

Reads each track's ANLZ file and saves a JSON blob into `enriched_tracks_analysis.rk_analysis_json`:

| Source | Field |
|---|---|
| PSSI tag | `mood_id`, `mood_name` (Low / Mid / High EDM); per-phrase `{kind_id, label, start_beat, end_beat, length_beats, start_sec, end_sec}` with semantic labels (Intro / Verse / Bridge / Chorus / Outro for Mood Low/Mid; Intro / Up / Down / Chorus / Outro for Mood High) |
| PCO2 / PCOB tags | `memory_cues[]` and `hot_cues[]`, each with `{time_sec, loop_time_sec, name, color_id, type_id}` — rekordbox auto-places its own based on Mood; usually more numerous and more useful than MIK's 8 |
| `DjmdContent.BPM` | `rekordbox_bpm` (rekordbox's own beatgrid BPM, may differ from `tempo_precise` / `ai-beatgrid`) |
| ANLZ tag list | `tags_seen` — diagnostic list of all tags found |

**Idempotent:** skip rule is `rekordbox_export_at IS NOT NULL AND rekordbox_analysis_at IS NULL`. Tracks not yet pushed are skipped (run `export-to-rekordbox` first). Tracks pushed but not yet analysed in rekordbox produce a partial blob and are NOT marked complete — re-run after analysing.

**Prerequisite:** rekordbox must be quit.

```bash
uv run dj_cli.py detect import-rekordbox-analysis --verbose
```

Sample `rk_analysis_json`:

```json
{
  "version": 1,
  "rekordbox_track_id": "12345",
  "mood_id": 3,
  "mood_name": "High (EDM)",
  "phrases": [
    {"index": 0, "kind_id": 1, "label": "Intro", "start_beat": 1, "end_beat": 32, "length_beats": 31, "start_sec": 0.0, "end_sec": 14.42},
    {"index": 1, "kind_id": 2, "label": "Up", "start_beat": 32, "end_beat": 64, "length_beats": 32, "start_sec": 14.42, "end_sec": 28.84},
    {"index": 2, "kind_id": 4, "label": "Chorus", "start_beat": 64, "end_beat": 128, "length_beats": 64, "start_sec": 28.84, "end_sec": 57.68}
  ],
  "memory_cues": [{"time_sec": 0.0, "name": "Intro", "color_id": 1}],
  "hot_cues":    [{"time_sec": 28.84, "name": "Drop", "color_id": 6}],
  "rekordbox_bpm": 129.18,
  "tags_seen": ["PCOB", "PCO2", "PQT2", "PQTZ", "PSSI", "PWAV", "PWV5", "PWV6"]
}
```

### End-to-end stages 5 + 6

```bash
# Stage 5 — quit DJ Studio first
uv run dj_cli.py detect studio-analyse --verbose

# Stage 6a — quit rekordbox first
uv run dj_cli.py detect export-to-rekordbox

# Stage 6 manual — open rekordbox → playlist → right-click → Analyze Tracks → quit

# Stage 6b
uv run dj_cli.py detect import-rekordbox-analysis --verbose
```

---

## Viewing enriched data

```bash
uv run dj_cli.py detect enriched              # all enriched tracks, newest first
uv run dj_cli.py detect enriched -n 100
uv run dj_cli.py detect enriched -p "Tech House"   # filter by Beatport playlist

uv run dj_cli.py detect enrich-runs           # past Stage 3 run summaries
uv run dj_cli.py detect enrich-runs -n 5

# Per-session enrichment status
uv run dj_cli.py detect enrich-tracks youtube 3              # session #3
uv run dj_cli.py detect enrich-tracks mixcloud 7
uv run dj_cli.py detect enrich-tracks youtube 3 --misses     # only not_found / fuzzy_miss
```

Use `detect sessions <type>` to find session IDs. `detect sessions <type> <id>` shows the raw `detected_tracks` for that scan; `detect enrich-tracks <type> <id>` shows the same set but with their Beatport-enrichment status joined in.

---

## playlist — SQL → Beatport / rekordbox

Take any SQL query that returns `beatport_id` and push the matching tracks to one of two destinations. The push code re-fetches each row via `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)` so artist/title/genre/key/bpm/length_ms are always available, regardless of how the user wrote their SQL.

```bash
# Beatport — creates the playlist if it doesn't exist; dedups against existing tracks
uv run dj_cli.py playlist beatport \
  --query "SELECT beatport_id FROM enriched_tracks WHERE genre='Tech House' AND bpm BETWEEN 124 AND 128 ORDER BY bpm" \
  --name "Peak Tech House"

# Rekordbox — creates the playlist; pushes bare Beatport streaming entries (FileType=20).
# Doesn't push cues. Quit rekordbox first.
# Filter on analysis-table columns by JOINing yourself:
uv run dj_cli.py playlist rekordbox \
  --query "SELECT e.beatport_id FROM enriched_tracks e JOIN enriched_tracks_analysis a USING(beatport_id) WHERE a.rk_analysis_json LIKE '%\"mood_name\":\"High%' LIMIT 30" \
  --name "High-mood set"

# Both accept --dry-run.
```

**Validation:** the query must start with `SELECT`. After fetch, if no `beatport_id` column is in the result set, the call errors. beatport_ids missing from `enriched_tracks` are reported and skipped.

**Difference from `detect export-to-rekordbox`:** that one is the idempotent Stage 6a that pushes everything in `enriched_tracks_analysis` where `rekordbox_export_at IS NULL` (i.e., already through `studio-analyse` but not yet pushed) and stamps the timestamp on success. `playlist rekordbox` is ad-hoc curation by SQL — no pipeline-stamp side effects, and it works against any track in `enriched_tracks` whether or not it's been through `studio-analyse`.

**Why no DJ Studio destination?** A previous `playlist dj-studio` destination wrote `projects-table/<uuid>` + `projects-meta-table/<uuid>` files, but DJ Studio also tracks per-mix UI state in IndexedDB (`~/Library/Application Support/DJ.Studio/IndexedDB/local-web_*.indexeddb.leveldb/`) that we couldn't write to — meaning UI delete was a no-op for tool-created mixes. We removed the destination rather than ship a half-working write path. DJ Studio is now read-only for this tool; assemble mixes in DJ Studio's UI.

---

## Environment variables

Copy `.env.example` to `.env` and set these before using `detect` or `sync`.

```
BEATPORT_ACCESS_TOKEN    Short-lived Bearer token (~10 min). Auto-refreshed via session token.
BEATPORT_SESSION_TOKEN   Long-lived NextAuth session cookie (~32 days). Used to refresh access token.

IG_USERNAME              Instagram username (for detect instagram)
IG_PASSWORD              Instagram password

MC_USERNAME              Mixcloud username (for detect mixcloud)
MC_PASSWORD              Mixcloud password

SPOTIFY_CLIENT_ID        Spotify app client ID (for detect gems --source spotify)
SPOTIFY_CLIENT_SECRET    Spotify app client secret

SOUNDCLOUD_CLIENT_ID     SoundCloud app client ID (for detect soundcloud + detect gems)
SOUNDCLOUD_CLIENT_SECRET SoundCloud app client secret
SOUNDCLOUD_REDIRECT_URI  OAuth callback URL (for detect login-soundcloud)

# Optional — only needed for headless browser login
BEATPORT_USERNAME        Beatport email
BEATPORT_PASSWORD        Beatport password
```

Get Beatport tokens manually if needed:
1. Open `beatport.com` in a browser (logged in)
2. DevTools → Network → find `/api/auth/session` → response JSON → copy `token.accessToken` → `BEATPORT_ACCESS_TOKEN`
3. DevTools → Application → Cookies → copy `__Secure-next-auth.session-token` (~3 KB value) → `BEATPORT_SESSION_TOKEN`

Or run `dj login-beatport --ui` and it does this automatically.

---

## Database schema

All tables live in `~/Music/dj-tools/dj.db`.

| Table | Written by | Contents |
|---|---|---|
| `detected_tracks` | Stage 2 (`detect`) | One row per unique track. `enrich_outcome` records miss state (`not_found`, `fuzzy_miss`). Deduped by Shazam key or artist+title. |
| `sessions` | Stage 2 (`detect`) | One row per unique URL scanned (youtube, mixcloud, soundcloud, radio, instagram, podbean, reddit, topdjmixes). Tracks scan progress and resume position. |
| `track_sessions` | Stage 2 (`detect`) | Junction: maps each track to the session(s) it appeared in, with timestamp position. |
| `gem_scans` | `detect gems` | One row per gems run: source, genre, requested/found counts, date window, linked `sessions` row. |
| `gem_tracks` | `detect gems` | Per-track gems metadata (url, release_date, plays, popularity) linking a `detected_tracks` row to a `gem_scans` row. Indexed on `(source, release_date)` for the cross-run dedup "fade" query. |
| `rejected_gems` | `detect gems` | Tracks the user rejected during gem review (source, artist, title, url, release_date). Excluded from future scans on that source. Indexed on `(source, release_date)` for the cross-run dedup "fade" query. |
| `enriched_tracks` | Stage 3 (`detect enrich`), Stage 4 (`detect sync-beatport`) | All Beatport-derived data on one row: id, detected_track_id, beatport_id, beatport_link, bpm, key, genre, release_date, artist, title, apple_music_url, enriched_at, plus the catalog-detail extras (mix_name, label, catalog_number, isrc, sub_genre, length_ms). |
| `enriched_tracks_analysis` | Stage 5 (`detect studio-analyse`) creates rows; Stage 6a/6b update them | Sparse — only tracks that have been through `studio-analyse`. Keyed on `beatport_id` (PK). Carries the DJ Studio analysis fields (mik_key, mik_nrg, mik_key_secondary, mik_key_confidence, tempo_precise, duration_sec, cue_points_count, vocals/drums/bass/melody {avg,peak}, analysis_json with full energy segments + 1Hz stem curves + per-segment stem RMS), rekordbox round-trip (rk_analysis_json), and per-stage timestamps (dj_studio_at, rekordbox_export_at, rekordbox_analysis_at). JOIN with `enriched_tracks` for the basic+catalog fields. |
| `enrich_runs` | Stage 3 (`detect enrich`) | Per-run summary: seen / found / not_found / fuzzy_miss / status. |
| `deleted_sessions` | `detect *-delete-session` | Audit log of deleted sessions. |
| `synced_tracks` | Stage 1 (`sync`) | Tracks synced to Beatport with outcome (added / duplicate / fuzzy_miss / no_classify). |
| `sync_runs` | Stage 1 (`sync`) | Per-run summary: seen / added / skipped / failed / status. |
| `auth_cache` | Stage 1 (`sync`) | Beatport Bearer token cache (service, token, captured_at, expires_at). |
| `cursors` | Stage 1 (`sync`) | Apple Music library incremental sync cursor (last `library_added_date` processed). |

---

## Helpers

```bash
# Rekordbox playlist cleanup — wipe a playlist + its tracks + cues
uv run helpers/cleanup_playlist.py --list
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --dry-run
uv run helpers/cleanup_playlist.py "Ibiza Vibes"
uv run helpers/cleanup_playlist.py "Ibiza Vibes" --delete-tracks

# Apple Music backup / restore
uv run helpers/backup_apple_music.py
uv run helpers/backup_apple_music.py --output ~/backup.json
uv run helpers/restore_apple_music.py --backup ~/backup.json --dry-run
uv run helpers/restore_apple_music.py --backup ~/backup.json

# Apple Music library tools
uv run helpers/export_apple_music.py         # CSV export
uv run helpers/clear_apple_music.py --dry-run
uv run helpers/clear_apple_music.py          # DESTRUCTIVE — clears library

# Delete a single track from a Beatport playlist
uv run helpers/delete_beatport_track.py \
  --track https://www.beatport.com/track/title/12345678 \
  --playlist "Tech House"
```

---

## Course viewer

Download and watch the Pete Tong DJ Academy / Circle course offline.
All course data lives under `~/Music/dj-tools/course/` (or an SSD — see below).

### Downloader

#### `login`

```bash
uv run helpers/download_course.py login <course_url>
```

Opens a headed (visible) browser window. Sign in manually. The session is saved
to a persistent browser profile at `~/Music/dj-tools/state/course-browser-profile/`
and reused by every subsequent `download` run — you only need to `login` once (or
after your session expires).

#### `download`

```bash
uv run helpers/download_course.py download <course_url> [flags]
```

Resumes from where it left off. For each lesson in course order it: navigates,
classifies the page type, extracts content (video, quiz, HTML, attachments),
clicks "Complete lesson" to unlock the next, and writes the manifest after each
lesson so progress survives interruption.

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--out-dir PATH` | `~/Music/dj-tools/course/` | Write all output (videos, manifest, quizzes, etc.) to a custom directory instead of the default. Use this to download a second course without clobbering the first. |
| `--limit N` | all | Stop after processing N lessons. The full manifest is still written for all discovered lessons; only the first N are actively scraped. Useful for smoke-testing after a code change. |
| `--dry-run` | off | Discover and print all lessons (title, ID, type, status) without downloading anything. No browser navigation, no file writes. |
| `--lesson-ids ID1,ID2,...` | all | Re-scrape only the listed lesson IDs, bypassing the normal "already complete" skip. Implies `force=True` for those lessons — quizzes are re-brute-forced from scratch even if `quizzes/<id>.json` already exists. Use after fixing a scraper bug, recovering a failed video, or re-running timed-out quizzes. |

**Skip logic** — a lesson is skipped (cached) unless `--lesson-ids` targets it:
- `extracted=True` AND `completed=True` AND video file present (or lesson has no video) → skip
- Otherwise → process

**Lesson types extracted:**

| Type | Description |
|------|-------------|
| `video_circle` | Circle-native HLS video: m3u8 captured via network sniffer, segments downloaded, muxed to mp4 |
| `video_dyntube` | Dyntube iframe video: AES-128 HLS key captured, manifest rewritten to local key URI, then downloaded |
| `quiz` | Multiple-choice quiz: brute-forced to find correct answers, saved to `quizzes/<id>.json` |
| `exercise` | Written exercise / assignment — HTML prose only, no video |
| `guide` | Reference / guide page — HTML prose only |
| `content` | Generic content page — HTML prose only |
| `locked` | Not yet unlocked on the platform (prior lesson incomplete) |
| `unknown` | Scraper couldn't classify the page (usually means it wasn't reached) |

**Common re-scrape recipes:**

```bash
# Re-run a single failed quiz
uv run helpers/download_course.py download <course_url> --lesson-ids 2569067

# Re-run several timed-out quizzes at once
uv run helpers/download_course.py download <course_url> --lesson-ids 2503039,2556782,2562957,2569067

# Re-scrape unknown/unextracted lessons
uv run helpers/download_course.py download <course_url> --lesson-ids 2623038,943070,943071

# Test the first 5 lessons only
uv run helpers/download_course.py download <course_url> --limit 5 --dry-run
```

**Output layout** under `~/Music/dj-tools/course/`:

```
lessons.json        full manifest — one entry per lesson
videos/             downloaded mp4 files
images/             lesson images
files/              lesson file attachments
quizzes/            quiz JSON (one file per quiz lesson)
thumbs/             video poster frames
subtitles/          VTT subtitle files
_keys/              captured AES-128 keys for Dyntube HLS videos
_hls/               rewritten m3u8 manifests (local key URIs)
failed.json         lessons that errored or timed out during the last run
```

Logs are written automatically to `~/Music/dj-tools/logs/download-course/YYYY-MM-DD_HHMMSS.log`.
The run holds a `caffeinate -i` power assertion so the Mac won't sleep mid-download.

### Viewer

```bash
cd helpers/course_viewer
npm install          # first time only
npm run dev          # opens http://localhost:5173 (or next free port)
```

Vite serves everything directly from `~/Music/dj-tools/course/` as static assets — no
server, no network requests during playback. Video position and lesson completion state
are saved to `localStorage`.

### Moving course files to an external SSD

The course directory is ~30 GB. To move it off the boot drive:

```bash
# 1. Move the files to the SSD (substitute your actual mount point)
mv ~/Music/dj-tools/course /Volumes/YourSSD/dj-course

# 2. Symlink the original path to the new location
ln -s /Volumes/YourSSD/dj-course ~/Music/dj-tools/course
```

The symlink is transparent to Vite, the downloader, and `paths.py` — nothing else needs
to change. Make sure the SSD is mounted before starting the viewer or running the
downloader.

---

## Tests

```bash
uv run pytest
```

---

## Package layout

```
dj_cli.py                       CLI entrypoint — detect / sync / playlist / login-beatport

connections/                    Transport layer — no app-specific dependencies
  beatport.py                   Beatport HTTP client + Playwright session token capture
  musickit.py                   Swift MusicKit bridge subprocess wrapper
  matching.py                   Fuzzy title/artist match against Beatport search results
  bridge/                       musickit_bridge.swift (compiled on first use, cached)

detect/                         Track detection + enrichment pipeline (Stages 2-6)
  db.py                         All detect + enrich DB operations
  cli.py                        argparse subcommands + async dispatch
  gems.py                       detect gems: low-play track discovery across Spotify/SoundCloud/Bandcamp/Beatport
  enrich.py                     Stage 3: detected → Beatport metadata (incl. full track-detail)
  sync_beatport.py              Stage 4: pull Beatport library → enriched_tracks
  studio_sdk.py                 Shared SDK driver: SdkHelper class, _shape_result,
                                token decrypt, failure sidecar
  dj_studio_sdk.js              Long-running Node helper (MIK WASM, ai-beatgrid,
                                ai-stems Demucs, cf.dj.studio classifier)
  studio_analyse.py             Stage 5: SDK analysis → enriched_tracks_analysis (DB only,
                                no DJ Studio filesystem writes)
  export_to_rekordbox.py        Stage 6a: pending → rekordbox playlist (idempotent)
  import_rekordbox_analysis.py  Stage 6b: ingest PSSI + cues from ANLZ files
  instagram.py / mixcloud.py / youtube.py / soundcloud.py / radio.py /
  podbean.py / reddit.py / topdjmixes.py
                                Stage 2: per-platform capture (Shazam for audio
                                sources, paste-into-vi for reddit / topdjmixes)
  shazam.py / parser.py         Audio recognition + tracklist parsing

sync/                           Stage 1: Apple Music → Beatport
  db.py / sync.py / classifier.py / cli.py

playlist/                       SQL-curated push to a destination
  query.py                      Run user SQL → list[beatport_id] + full row fetch
  to_beatport.py                Push to a Beatport playlist
  to_rekordbox.py               Push to a rekordbox playlist (also imported by Stage 6a)
  cli.py                        argparse subcommands

djstudio/                       Read DJ Studio project files + library (used for ad-hoc inspection)
  extractor.py                  audio-library-table + projects-table reader
  keys.py                       Camelot key conversion

rekordbox/                      Rekordbox writes via pyrekordbox
  backup.py                     master.db backup
  constants.py                  Path discovery + Camelot/cue-kind constants

helpers/                        Standalone maintenance scripts + course tools
  download_course.py            Course downloader (browser scrape + Dyntube/Circle HLS)
  course_viewer/                Vite + React viewer — serves ~/Music/dj-tools/course/ locally
```
