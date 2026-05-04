# dj

Unified DJ toolkit: move DJ.Studio mixes into rekordbox, detect tracks from any audio source via Shazam, and sync Apple Music playlists to Beatport.

All state lives in a single SQLite database at `~/Music/DJ.Studio/dj.db`. Log files are written to `~/Music/`.

---

## Setup

```bash
uv sync
uv run playwright install chromium   # needed for Beatport browser login
```

Copy `.env.example` to `.env` and fill in credentials before using `detect` or `sync`.

Rekordbox must be **closed** before any `export-studio` write.

---

## Command tree

```
dj
├── export-studio [target] [flags]    DJ.Studio mix → Rekordbox
├── login-beatport [--ui | --cookie]  Fetch and save a Beatport token
├── detect                            Track detection via Shazam
│   ├── instagram <url>               [--username] [--password] [--output] [--json]
│   ├── radio-garden <url>            [--interval N] [--capture N] [--duration N] [--cooldown N]
│   ├── mixcloud <url>                [--username] [--password] [--interval N] [--capture N]
│   ├── youtube <url>                 [--interval N] [--capture N] [--output] [--json]
│   ├── podbean <url>                 [--interval N] [--capture N] [--output] [--json]
│   ├── reddit <url>
│   ├── history                       [-n N]
│   ├── sessions <type>               [-n N]   types: youtube mixcloud radio instagram podbean reddit
│   ├── instagram-history             [--tracks] [-n N]
│   ├── radio-history                 [-n N]
│   ├── mixcloud-history              [-n N]
│   ├── mixcloud-delete-session <id>  [--force]
│   ├── youtube-history               [-n N]
│   ├── youtube-delete-session <id>   [--force]
│   ├── podbean-history               [-n N]
│   ├── podbean-delete-session <id>   [--force]
│   ├── reddit-history                [-n N]
│   ├── reddit-delete-session <id>    [--force]
│   ├── login-instagram               [--username] [--password]
│   ├── login-mixcloud                [--username] [--password]
│   ├── enrich                        [--dry-run] [--limit N] [--verbose] [--threshold F] [--retry-misses]
│   ├── sync-beatport                 [--dry-run] [--limit N] [--verbose]
│   ├── enrich-studio                 [--dry-run] [--limit N] [--verbose]
│   ├── enriched                      [-n N]
│   ├── enrich-runs                   [-n N]
│   └── enrich-tracks <type> <id>     [--misses]
└── sync
    └── music-beatport
        ├── check-connections
        ├── list-playlists
        └── sync                      [--playlist NAME] [--library] [--favorites]
                                      [--library-and-favorites] [--all]
                                      [--dry-run] [--limit N] [--verbose] [--threshold F]
```

---

## export-studio

Moves a DJ.Studio mix into rekordbox in two passes. Pass 1 writes tracks, playlist, and transition effects. Pass 2 snaps hot cues to the nearest downbeat after rekordbox has analyzed the files.

```bash
uv run dj_cli.py export-studio "Ibiza Vibes"                        # full pipeline
uv run dj_cli.py export-studio "Ibiza Vibes" --extract-only -o mix.json
uv run dj_cli.py export-studio mix.json                              # from existing JSON
uv run dj_cli.py export-studio mix.json --pass1-only --dry-run
uv run dj_cli.py export-studio mix.json --pass2-only                 # cues after analysis
uv run dj_cli.py export-studio mix.json --pass2-only --no-snap       # skip beatgrid snap
uv run dj_cli.py export-studio --list                                # list DJ.Studio mixes
```

### Hot cue layout

```
A = Prep cue (incoming)       E = Prep cue (outgoing)
B = Transition start          F = Transition start
C = Bass swap (if present)    G = Bass swap (if present)
D = Transition end            H = Transition end
```

Prep distance is genre-tuned: techno/trance = 16 bars, house/electronica = 8, DnB/trap = 4.

---

## login-beatport

Fetches a fresh Beatport `BEATPORT_ACCESS_TOKEN` and `BEATPORT_SESSION_TOKEN` and writes both to `.env`. Run this once to bootstrap auth; after that `enrich` and `sync` auto-refresh via the session token.

```bash
uv run dj_cli.py login-beatport          # auto: tries session cookie, then browser
uv run dj_cli.py login-beatport --ui     # open a visible browser window to log in
uv run dj_cli.py login-beatport --cookie # refresh via BEATPORT_SESSION_TOKEN only
```

**How `--ui` works:** opens a real browser window (Brave/Chrome if installed, else Chromium) with a persistent profile at `~/.playlist-syncer/browser-profile`. If you're already logged into Beatport in that profile, the token is grabbed immediately and the window closes. If not, log in and it closes once the session is detected.

**Token lifetime:** `BEATPORT_ACCESS_TOKEN` expires in ~10 minutes. `BEATPORT_SESSION_TOKEN` lasts ~32 days. As long as the session token is valid, `enrich` and `sync` refresh the access token automatically without user action.

---

## detect

Identifies tracks playing in Instagram posts, radio streams, Mixcloud mixes, YouTube videos, and Podbean episodes via Shazam. Results are stored in `dj.db`. Re-scanning the same URL never creates duplicate tracks — each track is stored once, identified by Shazam key or artist + title.

For Mixcloud, YouTube, and Podbean, interrupted scans are automatically resumed from where they left off.

### Detection

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

Instagram credentials come from `IG_USERNAME` / `IG_PASSWORD` in `.env` or via `detect login-instagram`.
Mixcloud credentials come from `MC_USERNAME` / `MC_PASSWORD` or via `detect login-mixcloud`.

Reddit requires no credentials — the public JSON API is used. Works on any subreddit text post whose body contains lines like `Artist - Title` or `1. Artist - Title (Mix) [Label]`. Labels in `[brackets]` and markdown links are stripped automatically.

### History and sessions

```bash
uv run dj_cli.py detect history             # all detected tracks, newest first
uv run dj_cli.py detect history -n 100

uv run dj_cli.py detect sessions youtube    # sessions list with track counts
uv run dj_cli.py detect sessions mixcloud
uv run dj_cli.py detect sessions radio
uv run dj_cli.py detect sessions instagram
uv run dj_cli.py detect sessions podbean

uv run dj_cli.py detect instagram-history           # grouped by post
uv run dj_cli.py detect instagram-history --tracks  # flat track list only
uv run dj_cli.py detect radio-history
uv run dj_cli.py detect mixcloud-history
uv run dj_cli.py detect youtube-history
uv run dj_cli.py detect podbean-history

uv run dj_cli.py detect mixcloud-delete-session <id>
uv run dj_cli.py detect mixcloud-delete-session <id> --force
uv run dj_cli.py detect youtube-delete-session <id>
uv run dj_cli.py detect podbean-delete-session <id>
uv run dj_cli.py detect reddit-history
uv run dj_cli.py detect reddit-delete-session <id>
```

### Enrichment

#### enrich — Beatport metadata

Fetches BPM, key, genre, release date, and Beatport link for all un-enriched detected tracks using fuzzy artist/title matching. Tracks with no results or score below threshold are marked on `detected_tracks.enrich_outcome` and skipped on future runs.

Requires `BEATPORT_ACCESS_TOKEN` and `BEATPORT_SESSION_TOKEN` in `.env`. Get them via `dj login-beatport`.

```bash
uv run dj_cli.py detect enrich                       # enrich all pending tracks
uv run dj_cli.py detect enrich --dry-run
uv run dj_cli.py detect enrich --limit 50
uv run dj_cli.py detect enrich --verbose             # print per-track Beatport detail
uv run dj_cli.py detect enrich --threshold 0.8       # stricter match (default: 0.72)
uv run dj_cli.py detect enrich --retry-misses        # retry previously missed tracks
```

Log written to `~/Music/YYYY-MM-DD_enrich_<run_id>.log`.

#### sync-beatport — pull from Beatport playlists

Pulls tracks from your Beatport library directly into `enriched_tracks`. Useful for seeding the DB with tracks you've already bought.

```bash
uv run dj_cli.py detect sync-beatport
uv run dj_cli.py detect sync-beatport --dry-run
uv run dj_cli.py detect sync-beatport --limit 100
uv run dj_cli.py detect sync-beatport --verbose
```

#### enrich-studio — DJ Studio metadata

Populates `mik_key`, `mik_nrg`, `vocals`, `drums`, `melody` from DJ Studio's audio library. Run after `enrich`. Skips tracks that already have `mik_key` set.

```bash
uv run dj_cli.py detect enrich-studio
uv run dj_cli.py detect enrich-studio --dry-run
uv run dj_cli.py detect enrich-studio --limit 50
uv run dj_cli.py detect enrich-studio --verbose
```

#### Viewing enriched data

```bash
uv run dj_cli.py detect enriched              # all enriched tracks, newest first
uv run dj_cli.py detect enriched -n 100

uv run dj_cli.py detect enrich-runs           # past run summaries
uv run dj_cli.py detect enrich-runs -n 5

# Enrichment status for every track in a session
uv run dj_cli.py detect enrich-tracks youtube 3     # session #3
uv run dj_cli.py detect enrich-tracks mixcloud 7
uv run dj_cli.py detect enrich-tracks youtube 3 --misses   # only not_found / fuzzy_miss
```

Use `detect sessions <type>` to find session IDs.

---

## sync

Syncs Apple Music tracks to Beatport genre playlists via fuzzy matching. Each track's outcome is recorded so it is never reprocessed. Interrupted runs resume cleanly.

Requires `BEATPORT_ACCESS_TOKEN` and `BEATPORT_SESSION_TOKEN` in `.env`. Get them via `dj login-beatport`.

Log written to `~/Music/YYYY-MM-DD_apple-music-sync_<run_id>.log`.

```bash
uv run dj_cli.py sync music-beatport check-connections

uv run dj_cli.py sync music-beatport list-playlists

# Pick one source per run
uv run dj_cli.py sync music-beatport sync --library                # library songs (incremental)
uv run dj_cli.py sync music-beatport sync --favorites              # Favourite Songs playlist
uv run dj_cli.py sync music-beatport sync --library-and-favorites  # union of both
uv run dj_cli.py sync music-beatport sync --all                    # all songs, no filter
uv run dj_cli.py sync music-beatport sync --playlist "Ibiza 2026"

# Common flags
uv run dj_cli.py sync music-beatport sync --library --dry-run
uv run dj_cli.py sync music-beatport sync --library --limit 100
uv run dj_cli.py sync music-beatport sync --library --verbose
uv run dj_cli.py sync music-beatport sync --library --threshold 0.85
```

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

# Optional — only needed if you want headless browser login
BEATPORT_USERNAME        Beatport email
BEATPORT_PASSWORD        Beatport password
```

Get Beatport tokens manually if needed:
1. Open `beatport.com` in a browser (logged in)
2. DevTools → Network → find `/api/auth/session` → response JSON → copy `token.accessToken` → `BEATPORT_ACCESS_TOKEN`
3. DevTools → Application → Cookies → copy `__Secure-next-auth.session-token` (~3 KB value) → `BEATPORT_SESSION_TOKEN`

Or just run `dj login-beatport --ui` and it does this automatically.

---

## Helpers

```bash
# Rekordbox playlist cleanup — wipe before re-running export-studio
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

## Database schema

All tables live in `~/Music/DJ.Studio/dj.db`.

| Table | Written by | Contents |
|---|---|---|
| `detected_tracks` | `detect` | One row per unique track. `enrich_outcome` records miss state (`not_found`, `fuzzy_miss`). Deduped by Shazam key or artist+title. |
| `sessions` | `detect` | One row per unique URL scanned (youtube, mixcloud, radio, instagram, podbean). Tracks scan progress and resume position. |
| `track_sessions` | `detect` | Junction: maps each track to the session(s) it appeared in, with timestamp position. |
| `enriched_tracks` | `detect enrich`, `detect sync-beatport` | Beatport-matched tracks: bpm, key, genre, release_date, beatport_id, beatport_link, apple_music_url, mik_key, mik_nrg, vocals, drums, melody. |
| `enrich_runs` | `detect enrich` | Per-run summary: seen / found / not_found / fuzzy_miss / status. |
| `deleted_sessions` | `detect *-delete-session` | Audit log of deleted sessions. |
| `synced_tracks` | `sync` | Tracks synced to Beatport with outcome (added / duplicate / fuzzy_miss / no_classify). |
| `sync_runs` | `sync` | Per-run summary: seen / added / skipped / failed / status. |
| `auth_cache` | `sync` | Beatport Bearer token cache (service, token, captured_at, expires_at). |
| `cursors` | `sync` | Apple Music library incremental sync cursor (last `library_added_date` processed). |

---

## Package layout

```
dj_cli.py           CLI entrypoint — export-studio, detect, sync, login-beatport
pipeline.py         export-studio pipeline orchestration

connections/        Transport layer — no app-specific dependencies
  beatport.py       Beatport HTTP client + Playwright session token capture
  musickit.py       Swift MusicKit bridge subprocess wrapper
  matching.py       Fuzzy title/artist match against Beatport search results
  bridge/           musickit_bridge.swift (compiled on first use, cached)

detect/             Track detection pipeline
  db.py             All detect + enrich DB operations
  cli.py            argparse subcommands + async logic
  enrich.py         Beatport enrichment loop
  enrich_studio.py  DJ Studio enrichment (mik_key, mik_nrg, stems)
  sync_beatport.py  Pull Beatport playlist tracks into enriched_tracks
  instagram.py      Instagram media fetch
  mixcloud.py       Mixcloud download + metadata
  youtube.py        YouTube download via yt-dlp
  radio.py          Radio stream capture + audio slicing
  podbean.py        Podbean episode download
  shazam.py         Shazam audio recognition wrapper
  parser.py         Track list text parser (caption / comment)

sync/               Apple Music → Beatport sync pipeline
  db.py             synced_tracks, sync_runs, auth_cache, cursors
  sync.py           run_sync() — main sync loop
  classifier.py     Beatport genre → destination playlist mapping
  cli.py            argparse subcommands

djstudio/           Read DJ.Studio project files
rekordbox/          Write rekordbox encrypted SQLite via pyrekordbox
helpers/            Standalone maintenance scripts
```
