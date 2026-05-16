"""Hidden-gem track finder — low-play tech house across Spotify, SoundCloud, Bandcamp."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from detect import db as detect_db

console = Console()

Source = Literal["spotify", "soundcloud", "bandcamp", "beatport"]

DATE_LABELS = ["<1 month", "<6 months", "<1 year", "<3 years"]
DATE_DAYS   = [30, 180, 365, 1095]
DATE_KEYS   = ["1mo", "6mo", "1yr", "3yr"]

GENRES = ["Tech House"]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cutoff(days: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=days)


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    # ISO 8601 variants
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Bandcamp: "16 May 2026 07:51:29 GMT" or "04 Jun 2021 00:00:00 GMT"
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S GMT"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Year only / year-month
    if len(raw) == 4 and raw.isdigit():
        return datetime(int(raw), 1, 1, tzinfo=timezone.utc)
    if len(raw) == 7 and "-" in raw:
        try:
            y, m = raw.split("-")
            return datetime(int(y), int(m), 1, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _norm_date(raw: str) -> str | None:
    """Normalize any date string to YYYY-MM-DD, or None if unparseable."""
    dt = _parse_date(raw or "")
    return dt.strftime("%Y-%m-%d") if dt else None


def _key(artist: str, title: str) -> tuple[str, str]:
    """Dedup key for a track — case-folded, whitespace-trimmed (artist, title)."""
    return ((artist or "").strip().lower(), (title or "").strip().lower())


# ── Interactive prompts ────────────────────────────────────────────────────────


def prompt_source() -> Source:
    console.print("\n[bold]Source[/bold]")
    console.print("  [1] Spotify")
    console.print("  [2] SoundCloud")
    console.print("  [3] Bandcamp")
    console.print("  [4] Beatport")
    while True:
        raw = Prompt.ask("  Choice", default="1")
        if raw in ("1", "spotify"):
            return "spotify"
        if raw in ("2", "soundcloud"):
            return "soundcloud"
        if raw in ("3", "bandcamp"):
            return "bandcamp"
        if raw in ("4", "beatport"):
            return "beatport"
        console.print("[red]Enter 1, 2, 3, or 4.[/red]")


def prompt_genre() -> str:
    console.print("\n[bold]Genre[/bold]")
    for i, g in enumerate(GENRES, 1):
        console.print(f"  [{i}] {g}")
    while True:
        raw = Prompt.ask("  Choice", default="1")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(GENRES):
                return GENRES[idx]
        except ValueError:
            pass
        console.print("[red]Invalid choice.[/red]")


def prompt_count() -> int:
    return IntPrompt.ask("\n[bold]How many tracks?[/bold] (1–20)", default=10)


def prompt_date() -> int:
    console.print("\n[bold]Max track age[/bold]")
    for i, label in enumerate(DATE_LABELS, 1):
        console.print(f"  [{i}] {label}")
    while True:
        raw = Prompt.ask("  Choice", default="2")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(DATE_DAYS):
                return DATE_DAYS[idx]
        except ValueError:
            pass
        console.print("[red]Enter 1–4.[/red]")


# ── Spotify ────────────────────────────────────────────────────────────────────


def _spotify_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _spotify_playlist_tracks(playlist_id: str, headers: dict, cutoff: datetime,
                             max_pop: int, seen_ids: set, count: int,
                             exclude: set[tuple[str, str]],
                             max_pages: int = 4) -> list[dict]:
    """Pull low-popularity, recently-released tracks from a Spotify playlist.

    `exclude` holds (artist, title) keys already seen — prior gems and tracks
    collected earlier this run. Skipped tracks are added to it so the same
    track never surfaces twice across playlists.
    """
    results = []
    fields = "next,items(track(id,name,popularity,artists(name),album(release_date),external_urls(spotify)))"
    next_url: str | None = None
    for page in range(max_pages):
        if next_url:
            resp = httpx.get(next_url, headers=headers, timeout=15)
        else:
            resp = httpx.get(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                params={"limit": 50, "market": "US", "fields": fields},
                headers=headers, timeout=15,
            )
        if resp.status_code != 200:
            break
        data = resp.json()
        for item in data.get("items", []):
            t = item.get("track") or {}
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            if t.get("popularity", 100) > max_pop:
                continue
            release_raw = t.get("album", {}).get("release_date", "")
            release_dt = _parse_date(release_raw)
            if release_dt and release_dt < cutoff:
                continue
            artist = ", ".join(a.get("name", "") for a in t.get("artists", []))
            title = t.get("name", "—")
            key = _key(artist, title)
            if key in exclude:
                continue
            exclude.add(key)
            results.append({
                "artist": artist,
                "title": title,
                "popularity": t.get("popularity"),
                "release_date": _norm_date(release_raw),
                "url": t.get("external_urls", {}).get("spotify") or "—",
            })
            if len(results) >= count:
                return results
        next_url = data.get("next")
        if not next_url:
            break
    return results


def search_spotify_gems(genre: str, count: int, max_age_days: int,
                         exclude: set[tuple[str, str]]) -> list[dict]:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        console.print("[yellow]Spotify credentials not found in .env[/yellow]")
        client_id = Prompt.ask("  SPOTIFY_CLIENT_ID")
        client_secret = Prompt.ask("  SPOTIFY_CLIENT_SECRET", password=True)

    token = _spotify_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    cutoff = _cutoff(max_age_days)

    # Find playlists for the genre then mine them for low-pop recent tracks.
    # Spotify's genre: search filter doesn't surface new releases reliably;
    # editorial playlists are the most accurate source.
    with console.status(f"[dim]Finding Spotify playlists for {genre}…[/dim]"):
        pl_resp = httpx.get(
            "https://api.spotify.com/v1/search",
            params={"q": genre, "type": "playlist", "limit": 15, "market": "US"},
            headers=headers, timeout=15,
        )
        pl_resp.raise_for_status()
        playlists = [p for p in pl_resp.json().get("playlists", {}).get("items", []) if p]

    if not playlists:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()
    # Widen popularity threshold if needed to always return something
    for max_pop in (15, 25, 35):
        with console.status(
            f"[dim]Scanning {len(playlists)} playlists "
            f"(popularity ≤ {max_pop})…[/dim]"
        ):
            for pl in playlists:
                if len(results) >= count:
                    break
                batch = _spotify_playlist_tracks(
                    pl["id"], headers, cutoff, max_pop, seen_ids,
                    count - len(results), exclude,
                )
                results.extend(batch)
        if results:
            break

    return results[:count]


# ── SoundCloud ────────────────────────────────────────────────────────────────


def search_soundcloud_gems(genre: str, count: int, max_age_days: int,
                           exclude: set[tuple[str, str]]) -> list[dict]:
    from connections import soundcloud as sc_api  # noqa: PLC0415

    if not sc_api.has_credentials():
        raise RuntimeError(
            "SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET not set in .env.\n"
            "Add them or run `dj detect login-soundcloud` first."
        )

    token = sc_api._get_token()
    cutoff = _cutoff(max_age_days)

    results: list[dict] = []
    offset = 0
    next_href: str | None = None

    with console.status(f"[dim]Searching SoundCloud for {genre}…[/dim]"):
        while len(results) < count:
            if next_href:
                resp = httpx.get(
                    next_href,
                    headers={"Authorization": f"OAuth {token}"},
                    timeout=15,
                )
            else:
                # Use the public API (api.soundcloud.com), not api-v2 which is
                # the internal web-app endpoint and 403s with client credentials.
                resp = httpx.get(
                    "https://api.soundcloud.com/tracks",
                    params={
                        "q": genre,
                        "genres": genre.lower(),
                        "limit": 50,
                        "offset": offset,
                        "linked_partitioning": True,
                    },
                    headers={"Authorization": f"OAuth {token}"},
                    timeout=15,
                )
            if resp.status_code == 401:
                token = sc_api._force_new_token(token)
                next_href = None
                continue
            resp.raise_for_status()
            data = resp.json()
            # Public API returns a list directly; v1-style pagination uses next_href
            items = data if isinstance(data, list) else data.get("collection", [])
            if not items:
                break
            for t in items:
                plays = t.get("playback_count", 999_999)
                if plays >= 5000:
                    continue
                created_raw = t.get("created_at", "")
                created_dt = _parse_date(created_raw)
                if created_dt and created_dt < cutoff:
                    continue
                artist = t.get("user", {}).get("username", "—")
                title = t.get("title", "—")
                key = _key(artist, title)
                if key in exclude:
                    continue
                exclude.add(key)
                results.append({
                    "artist": artist,
                    "title": title,
                    "plays": plays,
                    "release_date": _norm_date(created_raw),
                    "url": t.get("permalink_url") or "—",
                })
                if len(results) >= count:
                    break
            next_href = data.get("next_href") if isinstance(data, dict) else None
            if not next_href:
                offset += 50
                if offset >= 400:  # cap results scanned (raised for dedup headroom)
                    break

    return results[:count]


# ── Bandcamp ──────────────────────────────────────────────────────────────────

_BC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _bc_discover(tag: str, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Fetch `tag`-filtered new releases from Bandcamp's discover_web endpoint.

    The older `discover/3/get_web` endpoint silently ignored its `tag` param
    (it always echoed `g=all`), so results spanned every genre. `discover_web`
    honours `tag_norm_names`, so results are genuinely tagged with the
    requested subgenre. Returns (items, next_cursor).
    """
    payload: dict = {
        "category_id": 0,
        "slice": "new",
        "tag_norm_names": [tag],
        "geoname_id": 0,
        "include_result_types": ["a"],
        "size": 50,
    }
    if cursor:
        payload["cursor"] = cursor
    resp = httpx.post(
        "https://bandcamp.com/api/discover/1/discover_web",
        json=payload,
        headers=_BC_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("__api_special__") == "exception":
        raise RuntimeError(f"Bandcamp discover error: {data.get('error_type')}")
    return data.get("results", []), data.get("cursor")


def search_bandcamp_gems(genre: str, count: int, max_age_days: int,
                         exclude: set[tuple[str, str]]) -> list[dict]:
    tag = genre.lower().replace(" ", "-")
    cutoff = _cutoff(max_age_days)

    results: list[dict] = []
    cursor: str | None = None
    pages = 0
    with console.status(f"[dim]Fetching Bandcamp '{tag}'-tagged releases…[/dim]"):
        while len(results) < count and pages < 10:
            try:
                items, cursor = _bc_discover(tag, cursor)
            except (httpx.HTTPStatusError, RuntimeError):
                break
            pages += 1
            if not items:
                break
            for item in items:
                # discover_web dates look like "2026-05-08 00:00:00 UTC"
                release_raw = (item.get("release_date") or "")[:10]
                release_dt = _parse_date(release_raw) if release_raw else None
                if release_dt and release_dt < cutoff:
                    continue
                url = (item.get("item_url") or "").split("?")[0]
                if not url:
                    continue
                artist = item.get("album_artist") or item.get("band_name") or ""
                title = item.get("title") or ""
                # Skip obvious tag-spam: entries where title/artist are
                # non-Latin (common spamming tactic on Bandcamp tag pages)
                if re.search(r"[^\x00-\x7FÀ-ɏḀ-ỿ]", artist + title):
                    continue
                key = _key(artist, title)
                if key in exclude:
                    continue
                exclude.add(key)
                results.append({
                    "artist": artist or "—",
                    "title": title or "—",
                    "plays": "N/A",
                    "release_date": _norm_date(release_raw),
                    "url": url,
                })
                if len(results) >= count:
                    break
            if not cursor:
                break

    return results[:count]


# ── Beatport ──────────────────────────────────────────────────────────────────

# Beatport genre IDs — see https://api.beatport.com/v4/catalog/genres/
_BEATPORT_GENRE_IDS = {"Tech House": 11}


def search_beatport_gems(genre: str, count: int, max_age_days: int,
                         exclude: set[tuple[str, str]]) -> list[dict]:
    """Find recent, non-Hype tracks in a genre via Beatport's v4 catalog.

    Beatport classifies genre precisely (`genre_id`), so results are true
    genre matches — unlike tag-based sources. Beatport exposes no play count, so the
    "hidden gem" proxy is: exclude Hype (paid-promotion) tracks, newest first.
    """
    genre_id = _BEATPORT_GENRE_IDS.get(genre)
    if genre_id is None:
        raise RuntimeError(
            f"No Beatport genre id mapped for '{genre}'. "
            f"Known: {', '.join(_BEATPORT_GENRE_IDS)}."
        )

    from connections.beatport import API_ROOT, Beatport, make_client  # noqa: PLC0415
    from detect.enrich import _get_token  # noqa: PLC0415

    cutoff = _cutoff(max_age_days)
    today = datetime.now(tz=timezone.utc)
    bp = Beatport(client=make_client(_get_token()))

    results: list[dict] = []
    page = 1
    with console.status(f"[dim]Searching Beatport for {genre}…[/dim]"):
        while len(results) < count and page <= 8:
            try:
                resp = bp._request("GET", f"{API_ROOT}/catalog/tracks/", params={
                    "genre_id": genre_id,
                    "order_by": "-publish_date",
                    "publish_date": f"{cutoff:%Y-%m-%d}:{today:%Y-%m-%d}",
                    "per_page": 50,
                    "page": page,
                })
            except Exception:
                break
            data = resp.json()
            items = data.get("results", [])
            if not items:
                break
            for t in items:
                # Hidden gem: skip Hype (label-paid promotion) tracks.
                if t.get("is_hype"):
                    continue
                artist = ", ".join(a.get("name", "") for a in (t.get("artists") or []))
                title = t.get("name", "—")
                dkey = _key(artist, title)
                if dkey in exclude:
                    continue
                exclude.add(dkey)
                slug, tid = t.get("slug", ""), t.get("id")
                url = (f"https://www.beatport.com/track/{slug}/{tid}"
                       if slug and tid else "—")
                key_obj = t.get("key") or {}
                cnum, clet = key_obj.get("camelot_number"), key_obj.get("camelot_letter")
                camelot = f"{cnum}{clet}" if cnum and clet else (key_obj.get("name") or "—")
                results.append({
                    "artist": artist or "—",
                    "title": title,
                    "bpm": t.get("bpm"),
                    "track_key": camelot,
                    "release_date": _norm_date(t.get("publish_date") or ""),
                    "url": url,
                })
                if len(results) >= count:
                    break
            if not data.get("next"):
                break
            page += 1

    return results[:count]


# ── Display ───────────────────────────────────────────────────────────────────


def _clean_url(url: str) -> str:
    """Strip UTM / tracking params from a URL."""
    from urllib.parse import urlparse, urlencode, parse_qs
    if not url or url == "—":
        return url
    p = urlparse(url)
    clean_qs = {k: v for k, v in parse_qs(p.query).items()
                if not k.startswith("utm_")}
    clean = p._replace(query=urlencode(clean_qs, doseq=True))
    return clean.geturl()


def _print_urls(tracks: list[dict], url_key: str = "url") -> None:
    console.print()
    for i, r in enumerate(tracks, 1):
        url = _clean_url(r.get(url_key) or "—")
        # Print as plain text (no markup) so & in URLs doesn't break Rich
        console.print(f"  {i}. ", end="", highlight=False)
        console.print(url, style="blue", highlight=False)


def _render_spotify(tracks: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    t.add_column("#",        style="dim", width=3)
    t.add_column("Artist",   min_width=20)
    t.add_column("Title",    min_width=22)
    t.add_column("Pop",      style="dim", width=4)
    t.add_column("Released", style="dim", width=11)
    for i, r in enumerate(tracks, 1):
        t.add_row(str(i), r["artist"], r["title"],
                  str(r["popularity"]), r.get("release_date") or "—")
    console.print(t)
    _print_urls(tracks)
    console.print(f"\n[dim]Spotify popularity 0–100; results scored ≤25 (proxy for low plays)[/dim]")


def _render_soundcloud(tracks: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    t.add_column("#",        style="dim", width=3)
    t.add_column("Artist",   min_width=20)
    t.add_column("Title",    min_width=22)
    t.add_column("Plays",    style="dim", width=7)
    t.add_column("Uploaded", style="dim", width=11)
    for i, r in enumerate(tracks, 1):
        t.add_row(str(i), r["artist"], r["title"],
                  str(r["plays"]), r.get("release_date") or "—")
    console.print(t)
    _print_urls(tracks)


def _render_bandcamp(tracks: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    t.add_column("#",        style="dim", width=3)
    t.add_column("Artist",   min_width=20)
    t.add_column("Title",    min_width=22)
    t.add_column("Plays",    style="dim", width=7)
    t.add_column("Released", style="dim", width=11)
    for i, r in enumerate(tracks, 1):
        t.add_row(str(i), r["artist"], r["title"],
                  str(r["plays"]), r.get("release_date") or "—")
    console.print(t)
    _print_urls(tracks)
    console.print(f"\n[dim]Bandcamp does not expose play counts — sorted by newest release[/dim]")


def _render_beatport(tracks: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    t.add_column("#",        style="dim", width=3)
    t.add_column("Artist",   min_width=16)
    t.add_column("Title",    min_width=18)
    t.add_column("BPM",      style="dim", width=4)
    t.add_column("Key",      style="dim", width=4)
    t.add_column("Released", style="dim", width=11)
    for i, r in enumerate(tracks, 1):
        t.add_row(str(i), r["artist"], r["title"],
                  str(r.get("bpm") or "—"), r.get("track_key") or "—",
                  r.get("release_date") or "—")
    console.print(t)
    _print_urls(tracks)
    console.print("\n[dim]Beatport has no public play count — Hype (paid-promo) tracks "
                  "filtered out; exact genre match, newest first[/dim]")


def _persist(source: str, genre: str, count: int, max_age_days: int,
             tracks: list[dict]) -> int:
    """Save a gems run: one sessions row + gem_scans row + detected/gem tracks.

    Returns the session id. Detected tracks are deduped globally by
    (artist, title) inside `insert_track`, so re-inserts are harmless.
    """
    slug = genre.lower().replace(" ", "-")
    stamp = datetime.now(tz=timezone.utc).isoformat()
    url = f"gems://{source}/{slug}?t={stamp}"
    title = f"Gems · {source.title()} · {genre}"
    session_id = detect_db.create_session("gems", url, title, uploader=source)
    scan_id = detect_db.create_gem_scan(session_id, source, genre, count, max_age_days)
    for t in tracks:
        track_id = detect_db.insert_track(
            {"artist": t.get("artist") or "", "title": t.get("title") or ""},
            source=source,
            session_id=session_id,
        )
        plays = t.get("plays")
        popularity = t.get("popularity")
        detect_db.insert_gem_track(
            track_id, scan_id, source,
            url=t.get("url"),
            release_date=t.get("release_date"),
            plays=plays if isinstance(plays, int) else None,
            popularity=popularity if isinstance(popularity, int) else None,
        )
    detect_db.finish_gem_scan(scan_id, len(tracks))
    detect_db.end_session(session_id)
    return session_id


def run_gems(
    source: Source | None = None,
    genre: str | None = None,
    count: int | None = None,
    max_age_days: int | None = None,
    no_save: bool = False,
) -> None:
    """Interactive gem finder — prompts for any missing args.

    With `no_save`, results are shown but not written to the DB — prior runs'
    dedup still applies (the exclude set is read-only), so testing doesn't
    pollute `detected_tracks`.
    """
    if source is None:
        source = prompt_source()
    if genre is None:
        genre = prompt_genre()
    if count is None:
        count = prompt_count()
        count = max(1, min(20, count))
    if max_age_days is None:
        max_age_days = prompt_date()

    console.print(
        f"\n[bold]Searching[/bold] {source.title()} · "
        f"[cyan]{genre}[/cyan] · "
        f"[green]{count} tracks[/green] · "
        f"released in last {max_age_days}d\n"
    )

    # Build the exclude set: prior gems on this platform that could still
    # surface in a search bounded by the current cutoff (older ones "fade").
    cutoff_str = _cutoff(max_age_days).strftime("%Y-%m-%d")
    exclude = detect_db.seen_gem_keys(source, cutoff_str)
    skipped = len(exclude)

    if source == "spotify":
        tracks = search_spotify_gems(genre, count, max_age_days, exclude)
        renderer = _render_spotify
    elif source == "soundcloud":
        tracks = search_soundcloud_gems(genre, count, max_age_days, exclude)
        renderer = _render_soundcloud
    elif source == "beatport":
        tracks = search_beatport_gems(genre, count, max_age_days, exclude)
        renderer = _render_beatport
    else:
        tracks = search_bandcamp_gems(genre, count, max_age_days, exclude)
        renderer = _render_bandcamp

    if not tracks:
        msg = "No new tracks found — try a wider date range or higher count."
        if skipped:
            msg += f" ({skipped} already-seen track(s) skipped.)"
        console.print(f"[yellow]{msg}[/yellow]")
        return

    if no_save:
        renderer(tracks)
        console.print(f"\n[dim]--no-save: {len(tracks)} track(s) shown, not persisted[/dim]")
    else:
        session_id = _persist(source, genre, count, max_age_days, tracks)
        renderer(tracks)
        console.print(f"\n[dim]Saved {len(tracks)} track(s) to DB (session #{session_id})[/dim]")
    if len(tracks) < count:
        note = f"Only {len(tracks)} of {count} requested found"
        if skipped:
            note += f"; {skipped} already-seen skipped"
        console.print(f"[yellow]{note} — try a wider date range.[/yellow]")
