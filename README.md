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

DJ Studio must be **closed** before `detect import-to-studio`.

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
   (Instagram, YouTube, Mixcloud, Radio Garden,              │
    Podbean, Reddit)                                         │
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
                              │  Stage 5a: dj detect import-to-studio
                              │            (writes only to DJ Studio's local files)
                              ↓
                      DJ Studio's audio-library-table
                              │
                              │  Stage 5b: dj detect enrich-studio
                              │            (reads DJ Studio's library, INSERTs row)
                              ↓
   enriched_tracks_analysis  ←  mik_key, mik_nrg, vocals, drums, melody  +  dj_studio_at
                              │
                              │  Stage 6a: dj detect export-to-rekordbox
                              │            [open rekordbox → Analyze Tracks → close]
                              │  Stage 6b: dj detect import-rekordbox-analysis
                              ↓
   enriched_tracks_analysis  +  rk_analysis_json (PSSI phrases + cues)
                              +  rekordbox_export_at, rekordbox_analysis_at
```

Each enrichment stage is idempotent. `enriched_tracks_analysis` carries per-stage timestamps (`dj_studio_at`, `rekordbox_export_at`, `rekordbox_analysis_at`); re-runs only pick up new work. You can stop at any stage — every stage is independently useful. A row exists in `enriched_tracks_analysis` only after `enrich-studio` has populated it; `enriched_tracks` carries everything Beatport-derived without any sibling rows in the analysis table.

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
│   ├── podbean <url>                                  [--interval N] [--capture N] [--output] [--json]
│   ├── reddit <url>
│   │
│   ├── history / sessions / *-history                 Inspect detection state
│   ├── *-delete-session <id>                          Remove a scan session
│   ├── login-instagram / login-mixcloud               Save credentials
│   │
│   ├── enrich                                         Stage 3: detected → Beatport metadata
│   │                                                  [--dry-run] [--limit N] [--verbose] [--threshold F] [--retry-misses]
│   ├── sync-beatport                                  Stage 4: Beatport library → enriched_tracks
│   │                                                  [--dry-run] [--limit N] [--verbose]
│   ├── import-to-studio                               Stage 5a: drive DJ Studio analysis headlessly
│   │                                                  [--limit N] [--verbose] [--force]
│   ├── repair-studio-library                          Maintenance: drop half-baked DJ Studio library entries
│   │                                                  [--dry-run] [--include-orphans]
│   ├── enrich-studio                                  Stage 5b: read DJ Studio library files
│   │                                                  [--dry-run] [--limit N] [--verbose] [--force]
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
    ├── rekordbox --query SQL --name NAME              Rekordbox playlist
    └── dj-studio --query SQL --name NAME              DJ Studio mix project file
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

Identifies tracks playing in Instagram posts, radio streams, Mixcloud mixes, YouTube videos, Podbean episodes, and Reddit text posts via Shazam. Results land in `detected_tracks` (one row per unique track, deduped by Shazam key or artist + title). Re-scanning the same URL never creates duplicates.

Mixcloud, YouTube, and Podbean scans auto-resume from where they left off if interrupted.

```bash
uv run dj_cli.py detect instagram https://www.instagram.com/p/XXXXX/

uv run dj_cli.py detect radio-garden https://radio.garden/listen/station-name
uv run dj_cli.py detect radio-garden <url> --interval 60    # check every 60s
uv run dj_cli.py detect radio-garden <url> --duration 120   # run for 2 hours

uv run dj_cli.py detect mixcloud https://www.mixcloud.com/djname/mixname/
uv run dj_cli.py detect youtube https://www.youtube.com/watch?v=XXXX
uv run dj_cli.py detect podbean https://www.podbean.com/ew/pb-XXXX
uv run dj_cli.py detect reddit https://www.reddit.com/r/HypeTracks/comments/XXXXX/post_title/
```

**Credentials:**
- Instagram: `IG_USERNAME` / `IG_PASSWORD` in `.env`, or `dj detect login-instagram`.
- Mixcloud: `MC_USERNAME` / `MC_PASSWORD`, or `dj detect login-mixcloud`.
- Reddit: none. Public JSON API. Works on any subreddit text post whose body contains `Artist - Title` lines (markdown links and `[brackets]` are stripped).

### History and sessions

```bash
uv run dj_cli.py detect history             # all detected tracks, newest first
uv run dj_cli.py detect history -n 100

uv run dj_cli.py detect sessions youtube       # session list with track counts
uv run dj_cli.py detect sessions mixcloud
uv run dj_cli.py detect sessions radio
uv run dj_cli.py detect sessions instagram
uv run dj_cli.py detect sessions podbean
uv run dj_cli.py detect sessions reddit

uv run dj_cli.py detect sessions podbean 24    # detected_tracks for one session, in a table
uv run dj_cli.py detect sessions youtube 7     # (Pos, Artist, Title, Apple Music URL, enrich_outcome)

uv run dj_cli.py detect instagram-history           # grouped by post
uv run dj_cli.py detect instagram-history --tracks  # flat track list only
uv run dj_cli.py detect radio-history
uv run dj_cli.py detect mixcloud-history
uv run dj_cli.py detect youtube-history
uv run dj_cli.py detect podbean-history
uv run dj_cli.py detect reddit-history

uv run dj_cli.py detect mixcloud-delete-session <id>
uv run dj_cli.py detect youtube-delete-session <id>
uv run dj_cli.py detect podbean-delete-session <id>
uv run dj_cli.py detect reddit-delete-session <id>
```

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

Two commands run in order:

1. **`import-to-studio`** drives DJ Studio's bundled SDK headlessly for tracks that aren't yet in DJ Studio's library. Writes the same `audio-library-table` + `track-structures-table` + 4 `compressedAudioView*` binaries DJ Studio writes when you analyse manually in the UI. **Writes only to DJ Studio's local files — does not touch our DB.**
2. **`enrich-studio`** reads those library files (plus anything you analysed manually in DJ Studio's UI) and creates rows in `enriched_tracks_analysis`. This is the only stage that creates rows in that table.

### import-to-studio — drive DJ Studio's analysis headlessly

Uses your DJ Studio account + the bundled SDK to fetch full Beatport tracks, run the same MIK + ai-beatgrid + ai-stems pipeline DJ Studio uses internally, and write real DJ Studio library entries — no UI interaction needed.

**Per track captured (in DJ Studio's filesystem):**

| Source | Output |
|---|---|
| `cf.dj.studio/mixedinkey/analyze` (via WASM features) | mikKey + secondary key + confidence, mikEnergy 1-10, EnergyLevelSegments, CuePoints |
| `@appmachine/ai-beatgrid` (TorchScript) | precise BPM, all beat positions, downbeat |
| `@appmachine/ai-stems` Demucs Fast | vocals/drums/bass/other separated → compressedAudioView amplitude tracks + per-stem RMS averages and peaks |
| Computed | 8-bar phraseData, beat→phrase/energy mapping, bar-accent markers |

(Beatport metadata — mix_name, label, catalog_number, ISRC, sub_genre, length_ms — was already fetched by Stage 3 and is on `enriched_tracks`.)

**Prerequisites:**
1. **Quit DJ Studio (Cmd+Q)** before running. Its SDK conflicts with ours on port 61894 + `.beatport/` cache locks. Pre-flight check aborts with a clear message if DJ Studio is running.
2. Sign into Beatport via DJ Studio's UI at least once (so `~/Music/DJ.Studio/.beatport/<userId>/` has cached OAuth state). One-time.
3. DJ Studio refresh token must be valid. If expired, open DJ Studio briefly to refresh, quit it, re-run.

**`cf.dj.studio`** is DJ Studio's Cloudflare-hosted classification API. The local WASM extracts pitch/energy features; the server classifies them into a Camelot key + 1-10 energy. Same flow the desktop app uses internally — bit-identical output. Auth uses your DJ Studio account JWT (decrypted from `encryptedToken-v2.dat` and refreshed via `app-services.dj.studio`).

This command runs `caffeinate -i` automatically — your Mac won't sleep mid-run. Same applies to `detect enrich` (sequential Beatport API calls) and `detect radio-garden` (indefinite monitoring loop).

```bash
# Small sanity-check batch
uv run dj_cli.py detect import-to-studio --limit 5 --verbose

# Full batch
uv run dj_cli.py detect import-to-studio --verbose
```

**Flags:**
- `--limit N`: stop after N tracks (0 = no limit).
- `--force`: re-process tracks even if they're already in DJ Studio's library.

**Idempotent:** skip rule is "library_key exists in DJ Studio's `audio-library-table` AND has `mikKey` set". DJ Studio's filesystem is the single source of truth for "this track is imported" — we no longer track this in our DB.

**Crash-safe writes:** the audio-library-table file (with `mikKey`) is the skip indicator and is written LAST, after track-structures + 4 compressedAudioView binaries. Ctrl-C between writes leaves the audio-library-table file absent → the next run reprocesses cleanly. No half-baked tracks marked done.

**JWT auto-refresh mid-run:** DJ Studio's access JWT lasts ~60 min. On the first 401 from `cf.dj.studio` the run re-decrypts `encryptedToken-v2.dat`, re-exchanges via `app-services.dj.studio`, pushes the fresh token down to the running Node helper (`setAccessJwt` command — no helper restart, no model reload), and retries the failed track. Long batches don't need babysitting. If the post-refresh retry also 401s, the run aborts with a clear message — that means `encryptedToken-v2.dat` itself is invalid (open DJ Studio, sign in, quit, re-run).

**Failure handling:** transient `cf.dj.studio` failures are auto-retried inside the Node helper (4 attempts, exponential backoff up to 9s). Tracks that still fail get a second pass at the end of the batch after a 5s pause. The summary distinguishes "written / recovered on retry / permanently failed" with per-track error reasons.

**Per-track timing:** ~30-50s per track on first run (SDK + model cold-start), ~25-30s steady-state. ~2GB peak memory (Demucs models). 100 tracks ≈ 50-60 minutes.

When you reopen DJ Studio after running, those tracks appear in your library fully analysed — same as if you'd added them to a mix and let DJ Studio process them.

### repair-studio-library — clean up half-baked DJ Studio library entries

Maintenance helper for `import-to-studio`. Finds entries in DJ Studio's `audio-library-table` with `mikKey` set but missing companion files (track-structures or any of the 4 compressedAudioView binaries), and deletes the audio-library-table file so the next `import-to-studio` reprocesses them with the full pipeline.

Most "half-baked" entries come from DJ Studio's own UI flows (Beatport browser preview, dragging into a mix without playing), not from our tool — DJ Studio writes a light analysis (mikKey + camelotKey + beatGrids) without computing stems. `import-to-studio`'s pipeline produces the strictly fuller analysis with stems.

```bash
uv run dj_cli.py detect repair-studio-library --dry-run             # report only
uv run dj_cli.py detect repair-studio-library                       # delete recoverable
uv run dj_cli.py detect repair-studio-library --include-orphans     # delete free orphans too
```

Three classifications, only the safe ones are deleted by default:

| Classification | What it is | Default action |
|---|---|---|
| **recoverable** | beatport_id is in `enriched_tracks` | deleted; next `import-to-studio` requeues it |
| **orphan, free** | not in `enriched_tracks`, not used by any saved mix | skipped (use `--include-orphans` to delete; data loss — no recovery path through this tool) |
| **orphan, in-use** | not in `enriched_tracks`, but referenced by a mix in `projects-table` | NEVER deleted (would leave a broken slot in your saved mix), even with `--include-orphans` |

### enrich-studio — read DJ Studio's library into enriched_tracks_analysis

Creates the `enriched_tracks_analysis` row for each enriched track that's present in DJ Studio's library. Reads `audio-library-table` for `mik_key` + `mik_nrg`, and `audio-library-compressedAudioView{Vocals,Drums,Melody}` for the per-stem categorical intensity. INSERT-or-UPDATE keyed on `beatport_id`; stamps `dj_studio_at` on insert.

For tracks already analysed by you in DJ Studio's UI, this just reads existing data. For tracks not in DJ Studio's library yet, run `import-to-studio` first.

```bash
uv run dj_cli.py detect enrich-studio
uv run dj_cli.py detect enrich-studio --dry-run
uv run dj_cli.py detect enrich-studio --limit 50
uv run dj_cli.py detect enrich-studio --verbose
```

### Stored in `enriched_tracks_analysis` after Stage 5

```
beatport_id          -- PRIMARY KEY (link to enriched_tracks via JOIN)
mik_key, mik_nrg     -- from audio-library-table
vocals, drums, melody -- categorical low/medium/high (from compressedAudioView*)
dj_studio_at         -- set on first INSERT
```

The schema also reserves columns for `mik_key_secondary`, `mik_key_confidence`, `tempo_precise`, `duration_sec`, `cue_points_count`, the per-stem `*_avg`/`*_peak` RMS floats, and `analysis_json` — these are populated only if a future enrich-studio enhancement reads them out of DJ Studio's library files. Today they stay NULL.

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
# Stage 5a — quit DJ Studio first
uv run dj_cli.py detect import-to-studio --verbose

# Stage 5b — read DJ Studio library files
uv run dj_cli.py detect enrich-studio --verbose

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

## playlist — SQL → Beatport / rekordbox / DJ Studio

Take any SQL query that returns `beatport_id` and push the matching tracks to one of three destinations. The push code re-fetches each row via `enriched_tracks LEFT JOIN enriched_tracks_analysis USING(beatport_id)` so artist/title/genre/key/bpm/length_ms are always available, regardless of how the user wrote their SQL.

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

# DJ Studio — writes a new mix project file with linear track ordering and empty
# autoEffects (open the mix in DJ Studio and add transitions yourself). Tracks
# must already be in DJ Studio's library — run `dj detect import-to-studio` first
# if any of the matched beatport_ids haven't been imported yet.
uv run dj_cli.py playlist dj-studio \
  --query "SELECT beatport_id FROM enriched_tracks WHERE bpm BETWEEN 124 AND 128" \
  --name "124-128 BPM warmup"

# All three accept --dry-run.
```

**Validation:** the query must start with `SELECT`. After fetch, if no `beatport_id` column is in the result set, the call errors. beatport_ids missing from `enriched_tracks` are reported and skipped.

**Difference from `detect export-to-rekordbox`:** that one is the idempotent Stage 6a that pushes everything in `enriched_tracks_analysis` where `rekordbox_export_at IS NULL` (i.e., already through enrich-studio but not yet pushed) and stamps the timestamp on success. `playlist rekordbox` is ad-hoc curation by SQL — no pipeline-stamp side effects, and it works against any track in `enriched_tracks` whether or not it's been through enrich-studio.

**`playlist dj-studio` writes two files per push:**
- `~/Music/DJ.Studio/Database/projects-table/<uuid>` — the full mix data (mixList, autoEffects, etc)
- `~/Music/DJ.Studio/Database/projects-meta-table/<uuid>` — the index entry the sidebar reads

DJ Studio also tracks per-mix UI state in IndexedDB (`~/Library/Application Support/DJ.Studio/IndexedDB/local-web_*.indexeddb.leveldb/`) which we don't write to. The mix appears in DJ Studio's mixes list (the loader rebuilds that list from disk on launch) and tracks load/play correctly, **but UI delete is a no-op for tool-created mixes** — the right-click → Delete flow looks up the IndexedDB row, doesn't find it, and silently fails. To remove a mix our tool created, delete both files manually:

```bash
KEY=<uuid printed by the playlist dj-studio command>
rm ~/Music/DJ.Studio/Database/projects-table/$KEY \
   ~/Music/DJ.Studio/Database/projects-meta-table/$KEY
# then quit + reopen DJ Studio
```

Use `playlist dj-studio` for ephemeral inspection mixes (where you're fine deleting via `rm` later); push keepers via DJ Studio's own UI.

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
| `sessions` | Stage 2 (`detect`) | One row per unique URL scanned (youtube, mixcloud, radio, instagram, podbean, reddit). Tracks scan progress and resume position. |
| `track_sessions` | Stage 2 (`detect`) | Junction: maps each track to the session(s) it appeared in, with timestamp position. |
| `enriched_tracks` | Stage 3 (`detect enrich`), Stage 4 (`detect sync-beatport`) | All Beatport-derived data on one row: id, detected_track_id, beatport_id, beatport_link, bpm, key, genre, release_date, artist, title, apple_music_url, enriched_at, plus the catalog-detail extras (mix_name, label, catalog_number, isrc, sub_genre, length_ms). |
| `enriched_tracks_analysis` | Stage 5b (`detect enrich-studio`) creates rows; Stage 6a/6b update them | Sparse — only tracks that have been through enrich-studio. Keyed on `beatport_id` (PK). Carries the DJ Studio analysis fields (mik_key, mik_nrg, vocals, drums, melody, plus reserved columns mik_key_secondary, mik_key_confidence, tempo_precise, duration_sec, cue_points_count, vocals/drums/bass/melody {avg,peak}, analysis_json), rekordbox round-trip (rk_analysis_json), and per-stage timestamps (dj_studio_at, rekordbox_export_at, rekordbox_analysis_at). JOIN with `enriched_tracks` for the basic+catalog fields. |
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
  enrich.py                     Stage 3: detected → Beatport metadata (incl. full track-detail)
  sync_beatport.py              Stage 4: pull Beatport library → enriched_tracks
  import_to_studio.py           Stage 5a: drive DJ Studio's bundled SDK headlessly;
                                writes ONLY to DJ Studio's local files
  dj_studio_sdk.js              Long-running Node helper for Stage 5a (MIK WASM,
                                ai-beatgrid, ai-stems Demucs, cf.dj.studio classifier)
  enrich_studio.py              Stage 5b: read DJ Studio library, INSERT into
                                enriched_tracks_analysis (creation point)
  export_to_rekordbox.py        Stage 6a: pending → rekordbox playlist (idempotent)
  import_rekordbox_analysis.py  Stage 6b: ingest PSSI + cues from ANLZ files
  instagram.py / mixcloud.py / youtube.py / radio.py / podbean.py / reddit.py
                                Stage 2: per-platform Shazam capture
  shazam.py / parser.py         Audio recognition + tracklist parsing

sync/                           Stage 1: Apple Music → Beatport
  db.py / sync.py / classifier.py / cli.py

playlist/                       SQL-curated push to a destination
  query.py                      Run user SQL → list[beatport_id] + full row fetch
  to_beatport.py                Push to a Beatport playlist
  to_rekordbox.py               Push to a rekordbox playlist (also imported by Stage 6a)
  to_djstudio.py                Write a DJ Studio mix project file
  cli.py                        argparse subcommands

djstudio/                       Read DJ Studio project files + library
  extractor.py                  audio-library-table loader (used by enrich-studio)
  keys.py                       Camelot key conversion

rekordbox/                      Rekordbox writes via pyrekordbox
  backup.py                     master.db backup
  constants.py                  Path discovery + Camelot/cue-kind constants

helpers/                        Standalone maintenance scripts
```
