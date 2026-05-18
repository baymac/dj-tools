"""Spotify playlist → detected_tracks importer.

Accepts a playlist URL (open.spotify.com/playlist/...) or a plain-text name.
Name input triggers an interactive search+pick flow. All tracks are saved
directly without audio download or Shazam — artist/title come from Spotify's
metadata.
"""

from __future__ import annotations

import os
import re

import httpx
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from detect import db as detect_db

console = Console()


def _get_credentials() -> tuple[str, str]:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        console.print("[yellow]Spotify credentials not found — set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env[/yellow]")
        client_id = Prompt.ask("  SPOTIFY_CLIENT_ID")
        client_secret = Prompt.ask("  SPOTIFY_CLIENT_SECRET", password=True)
    return client_id, client_secret


def _get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _playlist_id_from_url(text: str) -> str | None:
    m = re.search(r"spotify\.com/playlist/([A-Za-z0-9]+)", text)
    return m.group(1) if m else None


def _search_playlists(name: str, headers: dict) -> list[dict]:
    resp = httpx.get(
        "https://api.spotify.com/v1/search",
        params={"q": name, "type": "playlist", "limit": 10, "market": "US"},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return [p for p in resp.json().get("playlists", {}).get("items", []) if p]


def _fetch_playlist_info(playlist_id: str, headers: dict) -> tuple[str, str]:
    """Return (playlist_title, owner_display_name)."""
    resp = httpx.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("name", "Spotify Playlist"), data.get("owner", {}).get("display_name", "Spotify")


def _fetch_playlist_tracks(playlist_id: str, headers: dict) -> list[dict]:
    """Page through a playlist and return all non-null tracks as dicts."""
    tracks: list[dict] = []
    next_url: str | None = None
    page = 0

    while page <= 40:  # 2 000 tracks max
        if next_url:
            resp = httpx.get(next_url, headers=headers, timeout=15)
        else:
            resp = httpx.get(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                params={"limit": 50, "market": "US"},
                headers=headers,
                timeout=15,
            )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            if retry_after > 120:
                from datetime import datetime, timedelta, timezone
                until = datetime.now(tz=timezone.utc) + timedelta(seconds=retry_after)
                raise RuntimeError(
                    f"Spotify rate-limited for {retry_after // 3600}h "
                    f"{(retry_after % 3600) // 60}m — try again after "
                    f"{until.strftime('%H:%M UTC')}"
                )
            console.print(f"[yellow]  Rate limited — waiting {retry_after + 1}s…[/yellow]")
            import time; time.sleep(retry_after + 1)
            continue
        if resp.status_code != 200:
            break
        data = resp.json()
        for item in data.get("items", []):
            t = item.get("track") or {}
            if not t.get("id"):
                continue  # local / null tracks
            artist = ", ".join(a.get("name", "") for a in (t.get("artists") or []))
            title = t.get("name", "")
            if not artist or not title:
                continue
            tracks.append({"artist": artist, "title": title, "position": len(tracks) + 1})
        next_url = data.get("next")
        if not next_url:
            break
        page += 1

    return tracks


def run_spotify_playlist(url_or_name: str) -> None:
    """Import all tracks from a Spotify playlist into detected_tracks."""
    client_id, client_secret = _get_credentials()
    token = _get_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    playlist_id = _playlist_id_from_url(url_or_name)
    playlist_url = url_or_name if playlist_id else None

    if not playlist_id:
        with console.status(f'[dim]Searching Spotify for "{url_or_name}"…[/dim]'):
            playlists = _search_playlists(url_or_name, headers)

        if not playlists:
            console.print(f'[red]No playlists found for "{url_or_name}"[/red]')
            return

        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("#", style="dim", width=3)
        t.add_column("Name", min_width=30)
        t.add_column("Owner", min_width=20)
        t.add_column("Tracks", style="dim", width=7)
        for i, pl in enumerate(playlists, 1):
            t.add_row(
                str(i),
                pl.get("name", "—"),
                (pl.get("owner") or {}).get("display_name", "—"),
                str((pl.get("tracks") or {}).get("total", "?")),
            )
        console.print(t)

        choice = IntPrompt.ask("Pick a playlist", default=1) - 1
        if choice < 0 or choice >= len(playlists):
            console.print("[red]Invalid choice.[/red]")
            return

        chosen = playlists[choice]
        playlist_id = chosen["id"]
        playlist_url = (
            (chosen.get("external_urls") or {}).get("spotify")
            or f"spotify://playlist/{playlist_id}"
        )

    with console.status("[dim]Fetching playlist…[/dim]"):
        title, owner = _fetch_playlist_info(playlist_id, headers)
        tracks = _fetch_playlist_tracks(playlist_id, headers)

    if not tracks:
        console.print("[yellow]No tracks found in this playlist.[/yellow]")
        return

    console.print(f"\n[bold]{title}[/bold] by {owner} — {len(tracks)} track(s)\n")
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    t.add_column("#", style="dim", width=4)
    t.add_column("Artist", min_width=22)
    t.add_column("Title", min_width=30)
    for tk in tracks:
        t.add_row(str(tk["position"]), tk["artist"], tk["title"])
    console.print(t)

    session_id = detect_db.create_session("spotify", playlist_url, title, uploader=owner)
    for tk in tracks:
        detect_db.insert_track(tk, source="spotify", session_id=session_id)
    detect_db.end_session(session_id)

    console.print(f"\n[dim]Saved {len(tracks)} track(s) to DB (session #{session_id})[/dim]")
