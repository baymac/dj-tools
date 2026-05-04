"""Wire `dj detect ...` argparse subparsers — ported from typer track-detect CLI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from .instagram import (
    build_client,
    download_file,
    fetch_media,
    fetch_pinned_comment,
    fetch_top_comments,
    video_resources,
)
from .db import (
    create_session,
    delete_session,
    end_session,
    find_session,
    infer_last_position,
    insert_track,
    insert_tracks,
    list_sessions,
    list_tracks,
    migrate,
    tracks_for_session,
    tracks_for_session_enriched,
    update_session_progress,
)
from . import db as detect_db
from .parser import has_track_info, parse_tracks
from .reddit import extract_from_text as reddit_extract_from_text, open_editor_for_post as reddit_open_editor
from .shazam import RECOGNIZE_TIMEOUT, format_result, recognize_file

load_dotenv()

CONFIG_FILE = Path.home() / ".track_detect_config.json"
console = Console()


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {hint}: ").strip().lower()
    except KeyboardInterrupt:
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def _load_saved_credentials(service: str = "instagram") -> tuple[str, str] | tuple[None, None]:
    if not CONFIG_FILE.exists():
        return None, None
    data = json.loads(CONFIG_FILE.read_text())
    if service in data and isinstance(data[service], dict):
        return data[service].get("username"), data[service].get("password")
    if service == "instagram" and "username" in data:
        return data.get("username"), data.get("password")
    return None, None


def _save_credentials(username: str, password: str, service: str = "instagram") -> None:
    data: dict = {}
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
    if "username" in data and service not in data:
        data["instagram"] = {"username": data.pop("username"), "password": data.pop("password", "")}
    data[service] = {"username": username, "password": password}
    CONFIG_FILE.write_text(json.dumps(data))
    CONFIG_FILE.chmod(0o600)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _render_text_tracks(tracks: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    table.add_column("#", style="dim", width=4)
    table.add_column("Artist", min_width=22)
    table.add_column("Title", min_width=28)
    for t in tracks:
        table.add_row(str(t.get("position", "")), t.get("artist", "—"), t.get("title", "—"))
    console.print(table)


def _render_shazam_tracks(tracks: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    table.add_column("Slide", style="dim", width=6)
    table.add_column("Artist", min_width=22)
    table.add_column("Title", min_width=28)
    table.add_column("Apple Music", min_width=40)
    for t in tracks:
        table.add_row(
            str(t.get("position", "")), t.get("artist", "—"), t.get("title", "—"),
            t.get("apple_music_url") or "—",
        )
    console.print(table)


def _fmt_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _render_mix_tracks(tracks: list[dict]) -> None:
    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    table.add_column("Time", style="dim", width=8)
    table.add_column("Artist", min_width=22)
    table.add_column("Title", min_width=28)
    table.add_column("Apple Music", min_width=40)
    for t in tracks:
        pos = t.get("position")
        table.add_row(
            _fmt_time(pos) if isinstance(pos, int) else "—",
            t.get("artist", "—"), t.get("title", "—"),
            t.get("apple_music_url") or "—",
        )
    console.print(table)


# ──────────────────────────────────────────────────────────────────────────────
# Core async logic — Instagram
# ──────────────────────────────────────────────────────────────────────────────


def _challenge_handler(username: str, choice: int) -> str:
    method = "email" if choice == 1 else "SMS/phone"
    console.print(f"\n[yellow]Instagram verification required.[/yellow] Check your {method} for a code.")
    return input("Verification code: ")


def _two_factor_handler() -> str:
    console.print("\n[yellow]Two-factor authentication required.[/yellow]")
    return input("2FA code: ")


async def _run(
    url: str,
    username: str,
    password: str,
    output: Optional[str],
    json_output: bool,
) -> None:
    console.print("[dim]Logging into Instagram…[/dim]")
    try:
        cl = build_client(username, password,
                          challenge_handler=_challenge_handler,
                          two_factor_handler=_two_factor_handler)
    except Exception as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        sys.exit(1)
    console.print("[green]✓[/green] Logged in")

    with console.status("[bold green]Fetching post…"):
        try:
            media = fetch_media(cl, url)
        except Exception as exc:
            console.print(f"[red]Could not fetch post:[/red] {exc}")
            sys.exit(1)

    media_type_label = {1: "photo", 2: "video", 8: "carousel"}.get(media.media_type, str(media.media_type))
    console.print(f"[green]✓[/green] Post [dim]{media.pk}[/dim] · type: {media_type_label}")

    caption: str = media.caption_text or ""
    tracks: list[dict] = []
    source = ""

    if caption:
        preview = caption[:180].replace("\n", " ")
        console.print(f"\n[dim]Caption:[/dim] {preview}{'…' if len(caption) > 180 else ''}")
        if has_track_info(caption):
            tracks = parse_tracks(caption)
            source = "caption"

    if not tracks:
        with console.status("[bold green]Checking comments…"):
            pinned = fetch_pinned_comment(cl, str(media.pk))
            comment_text = ""
            if pinned:
                comment_text = pinned.text or ""
                console.print(f"[dim]Pinned comment:[/dim] {comment_text[:180]}")
            else:
                top = fetch_top_comments(cl, str(media.pk), n=5)
                for c in top:
                    if has_track_info(c.text or ""):
                        comment_text = c.text
                        console.print(f"[dim]Comment with tracks:[/dim] {comment_text[:180]}")
                        break

        if comment_text and has_track_info(comment_text):
            tracks = parse_tracks(comment_text)
            source = "comment"

    if tracks:
        console.print(f"\n[bold]Found {len(tracks)} track(s) from {source}:[/bold]")
        _render_text_tracks(tracks)
    else:
        console.print("\n[yellow]No track list found in text — falling back to Shazam audio recognition…[/yellow]")
        tracks = await _shazam_slides(cl, media)
        source = "shazam"
        if tracks:
            console.print(f"\n[bold]Identified {len(tracks)} track(s) via Shazam:[/bold]")
            _render_shazam_tracks(tracks)
        else:
            console.print("[red]Could not identify any tracks.[/red]")

    if tracks:
        shortcode = url.split("/p/")[-1].split("/")[0].split("?")[0]
        session_id = create_session("instagram", url, shortcode, caption=caption or None)
        insert_tracks(tracks, source="instagram", session_id=session_id)
        console.print(f"\n[dim]Saved to DB (session #{session_id})[/dim]")

    if json_output:
        console.print_json(json.dumps(tracks, ensure_ascii=False))

    if output:
        Path(output).write_text(json.dumps(tracks, indent=2, ensure_ascii=False))
        console.print(f"\n[green]✓[/green] Saved to {output}")


async def _shazam_slides(cl, media) -> list[dict]:
    videos = video_resources(media)
    if not videos:
        console.print("[yellow]Post has no video slides to analyze.[/yellow]")
        return []

    results: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, resource in enumerate(videos, start=1):
            video_url = str(getattr(resource, "video_url", "") or "")
            if not video_url:
                continue

            dest = str(Path(tmpdir) / f"slide_{i}.mp4")
            console.print(f"  Slide {i}: [dim]downloading…[/dim]")
            try:
                download_file(video_url, dest)
            except Exception as exc:
                console.print(f"  Slide {i}: [red]download failed — {exc}[/red]")
                continue

            console.print(f"  Slide {i}: [dim]recognizing…[/dim]")
            try:
                raw = await recognize_file(dest)
                track = format_result(raw)
            except Exception as exc:
                console.print(f"  Slide {i}: [red]Shazam error — {exc}[/red]")
                continue

            if track.get("title"):
                track["position"] = i
                results.append(track)
                am = track.get("apple_music_url") or "no Apple Music link"
                console.print(f"  Slide {i}: [green]{track['artist']} — {track['title']}[/green]  {am}")
            else:
                console.print(f"  Slide {i}: [yellow]not recognized[/yellow]")

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Core async logic — Radio
# ──────────────────────────────────────────────────────────────────────────────


async def _run_radio(url: str, *, interval: int, capture_s: int, duration_min: int, cooldown: int) -> None:
    from .radio import capture_chunk, resolve_station

    with console.status("Resolving stream URL…"):
        try:
            stream_url, station_name = resolve_station(url)
        except ValueError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)

    console.print(f"[green]✓[/green] Station: [bold]{station_name}[/bold]")
    console.print(f"  Stream:  [dim]{stream_url}[/dim]")

    session_id = create_session("radio", stream_url, station_name)
    console.print(f"  Session [bold]#{session_id}[/bold] started")

    if duration_min:
        console.print(
            f"  Monitoring for [bold]{duration_min} min[/bold] "
            f"(capture: {capture_s}s every {interval}s, cooldown: {cooldown}s)"
        )
    else:
        console.print(
            f"  Press [bold]Ctrl+C[/bold] to stop  "
            f"(capture: {capture_s}s every {interval}s, cooldown: {cooldown}s)"
        )

    recent: dict[str, float] = {}
    total_checked = 0
    total_saved = 0
    stop_at = time.monotonic() + duration_min * 60 if duration_min else None
    last_saved_id: int | None = None
    last_saved_mono: float = 0.0
    CONSECUTIVE_GAP = 30.0

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            iteration = 0
            while True:
                if stop_at and time.monotonic() >= stop_at:
                    break

                iteration += 1
                t_start = time.monotonic()
                loop = asyncio.get_running_loop()
                track: dict = {}
                chunk_path = str(Path(tmpdir) / f"chunk_{iteration}.mp3")

                capture_failed = False
                with Progress(
                    SpinnerColumn(),
                    TextColumn(f"  [dim][{iteration}] Capturing[/dim]"),
                    BarColumn(bar_width=28),
                    TextColumn("[dim]{task.completed:.0f}/{task.total:.0f}s[/dim]"),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as prog:
                    task_id = prog.add_task("", total=float(capture_s))
                    capture_future = loop.run_in_executor(
                        None, capture_chunk, stream_url, capture_s, chunk_path
                    )
                    t0 = time.monotonic()
                    while not capture_future.done():
                        prog.update(task_id, completed=min(time.monotonic() - t0, float(capture_s)))
                        await asyncio.sleep(0.2)
                    try:
                        await capture_future
                    except subprocess.CalledProcessError as exc:
                        capture_failed = True
                        stderr = (exc.stderr or b"").decode(errors="replace").strip()
                        last_line = stderr.splitlines()[-1] if stderr else "unknown error"
                        console.print(f"  [{iteration}] [red]Capture failed:[/red] {last_line}")
                    except subprocess.TimeoutExpired:
                        capture_failed = True
                        console.print(f"  [{iteration}] [red]Capture timed out[/red]")

                if capture_failed:
                    await asyncio.sleep(max(0, interval - (time.monotonic() - t_start)))
                    continue

                slice_size = 10
                windows: list[tuple[str, str]] = [
                    (chunk_path, f"[{iteration}] full {capture_s}s"),
                ]
                for start in range(0, capture_s, slice_size):
                    slice_path = str(Path(tmpdir) / f"chunk_{iteration}_{start}s.mp3")
                    windows.append((slice_path, f"[{iteration}] slice {start}–{start + slice_size}s"))

                from .radio import slice_audio
                for idx, (audio_path, label) in enumerate(windows):
                    if idx > 0:
                        start = (idx - 1) * slice_size
                        try:
                            slice_audio(chunk_path, start, slice_size, audio_path)
                        except subprocess.CalledProcessError:
                            console.print(f"  {label} [red]Slice failed[/red]")
                            continue

                    try:
                        with console.status(f"  [dim]{label} Recognizing…[/dim]"):
                            raw = await asyncio.wait_for(recognize_file(audio_path), timeout=30.0)
                        track = format_result(raw)
                    except asyncio.TimeoutError:
                        console.print(f"  {label} [yellow]Shazam timeout ({RECOGNIZE_TIMEOUT}s)[/yellow]")
                        break
                    except Exception as exc:
                        console.print(f"  {label} [yellow]Shazam error ({type(exc).__name__}): {exc}[/yellow]")
                        break

                    total_checked += 1

                    if track.get("title"):
                        break

                    if idx < len(windows) - 1:
                        console.print(f"  [dim]{label} not recognized — trying shorter slice…[/dim]")
                    else:
                        console.print(f"  [dim]{label} not recognized[/dim]")

                if track.get("title"):
                    key = track.get("shazam_key") or f"{track.get('artist')}:{track.get('title')}"
                    now_mono = time.monotonic()
                    last_seen = recent.get(key)

                    if last_seen is not None and (now_mono - last_seen) < cooldown:
                        remaining = int(cooldown - (now_mono - last_seen))
                        console.print(
                            f"  [dim]{track['artist']} — {track['title']}"
                            f"  (still playing, cooldown {remaining}s remaining)[/dim]"
                        )
                    else:
                        recent[key] = now_mono
                        new_id = insert_track(track, source="radio", session_id=session_id)
                        last_saved_id = new_id
                        last_saved_mono = now_mono
                        total_saved += 1
                        am = track.get("apple_music_url") or ""
                        console.print(
                            f"  [green bold]NEW[/green bold]  "
                            f"[bold]{track['artist']}[/bold] — {track['title']}"
                            + (f"  [dim]{am}[/dim]" if am else "")
                        )

                elapsed = time.monotonic() - t_start
                sleep_for = max(0.0, interval - elapsed)
                if sleep_for > 1:
                    console.print(f"  [dim]Next check in {sleep_for:.0f}s…[/dim]")
                    await asyncio.sleep(sleep_for)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        end_session(session_id)
        console.print(
            f"\n[bold]Session #{session_id} ended.[/bold]  "
            f"Checked {total_checked} windows, saved [green]{total_saved}[/green] new tracks."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Core async logic — Mixcloud
# ──────────────────────────────────────────────────────────────────────────────


async def _run_mixcloud(
    url: str,
    username: str | None,
    password: str | None,
    scan_interval: int,
    capture_s: int,
    output: str | None,
    json_output: bool,
    resume_session_id: int | None = None,
    resume_from: int = 0,
) -> None:
    from .mixcloud import audio_duration, download_mix, resolve_mix
    from .radio import slice_audio

    with console.status("Resolving mix info…"):
        try:
            mix_title, uploader, duration = resolve_mix(url, username, password)
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)

    console.print(f"[green]✓[/green] Mix: [bold]{mix_title}[/bold]")
    if uploader:
        console.print(f"  Uploader: [dim]{uploader}[/dim]")
    if duration:
        n_checks = max(1, duration // scan_interval)
        console.print(
            f"  Duration: [dim]{_fmt_time(duration)}[/dim]  "
            f"→ ~{n_checks} slices (every {scan_interval}s, {capture_s}s each)"
        )

    if resume_session_id is not None:
        session_id = resume_session_id
        console.print(
            f"  [yellow]Resuming session [bold]#{session_id}[/bold] "
            f"from {_fmt_time(resume_from)}[/yellow]"
        )
        prior_tracks = tracks_for_session(session_id)
        seen_keys: set[str] = {
            r["shazam_key"] or f"{r['artist']}:{r['title']}"
            for r in prior_tracks if r["shazam_key"] or r["title"]
        }
        all_tracks: list[dict] = [dict(r) for r in prior_tracks]
    else:
        session_id = create_session("mixcloud", url, mix_title, uploader or None, duration)
        console.print(f"  Session [bold]#{session_id}[/bold] started")
        seen_keys = set()
        all_tracks = []

    total_checked = 0
    total_saved = 0

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            console.print("")
            with console.status("[bold green]Downloading mix (may take a minute)…[/bold green]"):
                try:
                    mix_path = download_mix(url, tmpdir, username, password)
                except RuntimeError as exc:
                    console.print(f"[red]Download failed:[/red] {exc}")
                    sys.exit(1)

            console.print(f"[green]✓[/green] Downloaded: [dim]{mix_path.name}[/dim]")

            if not duration:
                duration = audio_duration(str(mix_path))
                if duration:
                    n_checks = max(1, duration // scan_interval)
                    console.print(
                        f"  Duration (from file): [dim]{_fmt_time(duration)}[/dim]  "
                        f"→ ~{n_checks} slices"
                    )

            all_positions = list(range(0, max(duration, 1), scan_interval))
            positions = [p for p in all_positions if p > resume_from] if resume_from else all_positions
            total_positions = len(positions)
            skipped = len(all_positions) - total_positions

            if skipped:
                console.print(f"\nSkipping {skipped} already-scanned position(s), scanning {total_positions} remaining…\n")
            else:
                console.print(f"\nScanning {total_positions} position(s)…\n")

            for i, pos in enumerate(positions, 1):
                slice_path = str(Path(tmpdir) / f"slice_{i}.mp3")
                label = f"[{i}/{total_positions}] @{_fmt_time(pos)}"

                try:
                    slice_audio(str(mix_path), pos, capture_s, slice_path)
                except subprocess.CalledProcessError:
                    console.print(f"  {label} [red]slice failed[/red]")
                    update_session_progress(session_id, pos)
                    continue

                try:
                    with console.status(f"  [dim]{label} Recognizing…[/dim]"):
                        raw = await recognize_file(slice_path)
                    track = format_result(raw)
                except asyncio.TimeoutError:
                    console.print(f"  {label} [yellow]Shazam timeout ({RECOGNIZE_TIMEOUT}s)[/yellow]")
                    update_session_progress(session_id, pos)
                    continue
                except Exception as exc:
                    console.print(f"  {label} [yellow]Shazam error: {exc}[/yellow]")
                    update_session_progress(session_id, pos)
                    continue

                total_checked += 1
                update_session_progress(session_id, pos)

                if not track.get("title"):
                    console.print(f"  [dim]{label} not recognized[/dim]")
                    continue

                key = track.get("shazam_key") or f"{track.get('artist')}:{track.get('title')}"
                if key in seen_keys:
                    console.print(f"  [dim]{label} {track['artist']} — {track['title']} (duplicate)[/dim]")
                    continue

                seen_keys.add(key)
                track["position"] = pos
                insert_track(track, source="mixcloud", session_id=session_id)
                total_saved += 1
                all_tracks.append(track)
                am = track.get("apple_music_url") or ""
                console.print(
                    f"  [green bold]FOUND[/green bold]  {label}  "
                    f"[bold]{track['artist']}[/bold] — {track['title']}"
                    + (f"  [dim]{am}[/dim]" if am else "")
                )

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        end_session(session_id)
        console.print(
            f"\n[bold]Session #{session_id} complete.[/bold]  "
            f"Checked {total_checked} slices, found [green]{total_saved}[/green] unique tracks."
        )

    if all_tracks:
        console.print("\n[bold]Tracklist:[/bold]")
        _render_mix_tracks(all_tracks)

    if json_output:
        console.print_json(json.dumps(all_tracks, ensure_ascii=False))

    if output:
        Path(output).write_text(json.dumps(all_tracks, indent=2, ensure_ascii=False))
        console.print(f"\n[green]✓[/green] Saved to {output}")


# ──────────────────────────────────────────────────────────────────────────────
# Core async logic — YouTube
# ──────────────────────────────────────────────────────────────────────────────


async def _run_youtube(
    url: str,
    scan_interval: int,
    capture_s: int,
    output: str | None,
    json_output: bool,
    resume_session_id: int | None = None,
    resume_from: int = 0,
) -> None:
    from .youtube import audio_duration, download_video, resolve_video
    from .radio import slice_audio

    with console.status("Resolving video info…"):
        try:
            video_title, uploader, duration = resolve_video(url)
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)

    console.print(f"[green]✓[/green] Video: [bold]{video_title}[/bold]")
    if uploader:
        console.print(f"  Uploader: [dim]{uploader}[/dim]")
    if duration:
        n_checks = max(1, duration // scan_interval)
        console.print(
            f"  Duration: [dim]{_fmt_time(duration)}[/dim]  "
            f"→ ~{n_checks} slices (every {scan_interval}s, {capture_s}s each)"
        )

    if resume_session_id is not None:
        session_id = resume_session_id
        console.print(
            f"  [yellow]Resuming session [bold]#{session_id}[/bold] "
            f"from {_fmt_time(resume_from)}[/yellow]"
        )
        prior_tracks = tracks_for_session(session_id)
        seen_keys: set[str] = {
            r["shazam_key"] or f"{r['artist']}:{r['title']}"
            for r in prior_tracks if r["shazam_key"] or r["title"]
        }
        all_tracks: list[dict] = [dict(r) for r in prior_tracks]
    else:
        session_id = create_session("youtube", url, video_title, uploader or None, duration)
        console.print(f"  Session [bold]#{session_id}[/bold] started")
        seen_keys = set()
        all_tracks = []

    total_checked = 0
    total_saved = 0

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            console.print("")
            with console.status("[bold green]Downloading video audio (may take a minute)…[/bold green]"):
                try:
                    video_path = download_video(url, tmpdir)
                except RuntimeError as exc:
                    console.print(f"[red]Download failed:[/red] {exc}")
                    sys.exit(1)

            console.print(f"[green]✓[/green] Downloaded: [dim]{video_path.name}[/dim]")

            if not duration:
                duration = audio_duration(str(video_path))
                if duration:
                    n_checks = max(1, duration // scan_interval)
                    console.print(
                        f"  Duration (from file): [dim]{_fmt_time(duration)}[/dim]  "
                        f"→ ~{n_checks} slices"
                    )

            all_positions = list(range(0, max(duration, 1), scan_interval))
            positions = [p for p in all_positions if p > resume_from] if resume_from else all_positions
            total_positions = len(positions)
            skipped = len(all_positions) - total_positions

            if skipped:
                console.print(f"\nSkipping {skipped} already-scanned position(s), scanning {total_positions} remaining…\n")
            else:
                console.print(f"\nScanning {total_positions} position(s)…\n")

            for i, pos in enumerate(positions, 1):
                slice_path = str(Path(tmpdir) / f"slice_{i}.mp3")
                label = f"[{i}/{total_positions}] @{_fmt_time(pos)}"

                try:
                    slice_audio(str(video_path), pos, capture_s, slice_path)
                except subprocess.CalledProcessError:
                    console.print(f"  {label} [red]slice failed[/red]")
                    update_session_progress(session_id, pos)
                    continue

                try:
                    with console.status(f"  [dim]{label} Recognizing…[/dim]"):
                        raw = await recognize_file(slice_path)
                    track = format_result(raw)
                except asyncio.TimeoutError:
                    console.print(f"  {label} [yellow]Shazam timeout ({RECOGNIZE_TIMEOUT}s)[/yellow]")
                    update_session_progress(session_id, pos)
                    continue
                except Exception as exc:
                    console.print(f"  {label} [yellow]Shazam error: {exc}[/yellow]")
                    update_session_progress(session_id, pos)
                    continue

                total_checked += 1
                update_session_progress(session_id, pos)

                if not track.get("title"):
                    console.print(f"  [dim]{label} not recognized[/dim]")
                    continue

                key = track.get("shazam_key") or f"{track.get('artist')}:{track.get('title')}"
                if key in seen_keys:
                    console.print(f"  [dim]{label} {track['artist']} — {track['title']} (duplicate)[/dim]")
                    continue

                seen_keys.add(key)
                track["position"] = pos
                insert_track(track, source="youtube", session_id=session_id)
                total_saved += 1
                all_tracks.append(track)
                am = track.get("apple_music_url") or ""
                console.print(
                    f"  [green bold]FOUND[/green bold]  {label}  "
                    f"[bold]{track['artist']}[/bold] — {track['title']}"
                    + (f"  [dim]{am}[/dim]" if am else "")
                )

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        end_session(session_id)
        console.print(
            f"\n[bold]Session #{session_id} complete.[/bold]  "
            f"Checked {total_checked} slices, found [green]{total_saved}[/green] unique tracks."
        )

    if all_tracks:
        console.print("\n[bold]Tracklist:[/bold]")
        _render_mix_tracks(all_tracks)

    if json_output:
        console.print_json(json.dumps(all_tracks, ensure_ascii=False))

    if output:
        Path(output).write_text(json.dumps(all_tracks, indent=2, ensure_ascii=False))
        console.print(f"\n[green]✓[/green] Saved to {output}")


# ──────────────────────────────────────────────────────────────────────────────
# Core async logic — Podbean
# ──────────────────────────────────────────────────────────────────────────────


async def _run_podbean(
    url: str,
    scan_interval: int,
    capture_s: int,
    output: str | None,
    json_output: bool,
    resume_session_id: int | None = None,
    resume_from: int = 0,
) -> None:
    from .podbean import audio_duration, download_episode, resolve_episode
    from .radio import slice_audio

    with console.status("Resolving episode info…"):
        try:
            episode_title, podcast_name, duration = resolve_episode(url)
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)

    console.print(f"[green]✓[/green] Episode: [bold]{episode_title}[/bold]")
    if podcast_name:
        console.print(f"  Podcast: [dim]{podcast_name}[/dim]")
    if duration:
        n_checks = max(1, duration // scan_interval)
        console.print(
            f"  Duration: [dim]{_fmt_time(duration)}[/dim]  "
            f"→ ~{n_checks} slices (every {scan_interval}s, {capture_s}s each)"
        )

    if resume_session_id is not None:
        session_id = resume_session_id
        console.print(
            f"  [yellow]Resuming session [bold]#{session_id}[/bold] "
            f"from {_fmt_time(resume_from)}[/yellow]"
        )
        prior_tracks = tracks_for_session(session_id)
        seen_keys: set[str] = {
            r["shazam_key"] or f"{r['artist']}:{r['title']}"
            for r in prior_tracks if r["shazam_key"] or r["title"]
        }
        all_tracks: list[dict] = [dict(r) for r in prior_tracks]
    else:
        session_id = create_session("podbean", url, episode_title, podcast_name or None, duration)
        console.print(f"  Session [bold]#{session_id}[/bold] started")
        seen_keys = set()
        all_tracks = []

    total_checked = 0
    total_saved = 0

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            console.print("")
            with console.status("[bold green]Downloading episode (may take a minute)…[/bold green]"):
                try:
                    episode_path = download_episode(url, tmpdir)
                except RuntimeError as exc:
                    console.print(f"[red]Download failed:[/red] {exc}")
                    sys.exit(1)

            console.print(f"[green]✓[/green] Downloaded: [dim]{episode_path.name}[/dim]")

            if not duration:
                duration = audio_duration(str(episode_path))
                if duration:
                    n_checks = max(1, duration // scan_interval)
                    console.print(
                        f"  Duration (from file): [dim]{_fmt_time(duration)}[/dim]  "
                        f"→ ~{n_checks} slices"
                    )

            all_positions = list(range(0, max(duration, 1), scan_interval))
            positions = [p for p in all_positions if p > resume_from] if resume_from else all_positions
            total_positions = len(positions)
            skipped = len(all_positions) - total_positions

            if skipped:
                console.print(f"\nSkipping {skipped} already-scanned position(s), scanning {total_positions} remaining…\n")
            else:
                console.print(f"\nScanning {total_positions} position(s)…\n")

            for i, pos in enumerate(positions, 1):
                slice_path = str(Path(tmpdir) / f"slice_{i}.mp3")
                label = f"[{i}/{total_positions}] @{_fmt_time(pos)}"

                try:
                    slice_audio(str(episode_path), pos, capture_s, slice_path)
                except subprocess.CalledProcessError:
                    console.print(f"  {label} [red]slice failed[/red]")
                    update_session_progress(session_id, pos)
                    continue

                try:
                    with console.status(f"  [dim]{label} Recognizing…[/dim]"):
                        raw = await recognize_file(slice_path)
                    track = format_result(raw)
                except asyncio.TimeoutError:
                    console.print(f"  {label} [yellow]Shazam timeout ({RECOGNIZE_TIMEOUT}s)[/yellow]")
                    update_session_progress(session_id, pos)
                    continue
                except Exception as exc:
                    console.print(f"  {label} [yellow]Shazam error: {exc}[/yellow]")
                    update_session_progress(session_id, pos)
                    continue

                total_checked += 1
                update_session_progress(session_id, pos)

                if not track.get("title"):
                    console.print(f"  [dim]{label} not recognized[/dim]")
                    continue

                key = track.get("shazam_key") or f"{track.get('artist')}:{track.get('title')}"
                if key in seen_keys:
                    console.print(f"  [dim]{label} {track['artist']} — {track['title']} (duplicate)[/dim]")
                    continue

                seen_keys.add(key)
                track["position"] = pos
                insert_track(track, source="podbean", session_id=session_id)
                total_saved += 1
                all_tracks.append(track)
                am = track.get("apple_music_url") or ""
                console.print(
                    f"  [green bold]FOUND[/green bold]  {label}  "
                    f"[bold]{track['artist']}[/bold] — {track['title']}"
                    + (f"  [dim]{am}[/dim]" if am else "")
                )

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        end_session(session_id)
        console.print(
            f"\n[bold]Session #{session_id} complete.[/bold]  "
            f"Checked {total_checked} slices, found [green]{total_saved}[/green] unique tracks."
        )

    if all_tracks:
        console.print("\n[bold]Tracklist:[/bold]")
        _render_mix_tracks(all_tracks)

    if json_output:
        console.print_json(json.dumps(all_tracks, ensure_ascii=False))

    if output:
        Path(output).write_text(json.dumps(all_tracks, indent=2, ensure_ascii=False))
        console.print(f"\n[green]✓[/green] Saved to {output}")


# ──────────────────────────────────────────────────────────────────────────────
# Argparse CLI
# ──────────────────────────────────────────────────────────────────────────────


def add_detect_subparser(parent: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach `detect` and its subcommands to the parent subparsers."""
    detect_p = parent.add_parser(
        "detect",
        help="Detect tracks from Instagram, radio, Mixcloud, YouTube, Podbean via Shazam",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run dj_cli.py detect instagram https://www.instagram.com/p/XXXXX/
  uv run dj_cli.py detect radio-garden https://radio.garden/listen/kexp/kexp
  uv run dj_cli.py detect mixcloud https://www.mixcloud.com/djname/mix/
  uv run dj_cli.py detect youtube https://www.youtube.com/watch?v=XXXX
  uv run dj_cli.py detect podbean https://www.podbean.com/ew/pb-XXXX
  uv run dj_cli.py detect reddit https://www.reddit.com/r/HypeTracks/comments/XXXXX/
  uv run dj_cli.py detect history -n 50
  uv run dj_cli.py detect mixcloud-history
  uv run dj_cli.py detect reddit-history
""",
    )
    sub = detect_p.add_subparsers(dest="detect_command")

    # instagram
    ig_p = sub.add_parser("instagram", help="Detect tracks from an Instagram post")
    ig_p.add_argument("url", help="Instagram post URL")
    ig_p.add_argument("--username", "-u", default=None,
                      help="Instagram username (or set IG_USERNAME)")
    ig_p.add_argument("--password", "-p", default=None,
                      help="Instagram password (or set IG_PASSWORD)")
    ig_p.add_argument("--output", "-o", default=None, help="Write results to JSON file")
    ig_p.add_argument("--json", "-j", action="store_true", dest="json_output",
                      help="Print results as JSON to stdout")

    # radio-garden
    rg_p = sub.add_parser("radio-garden", help="Monitor a radio.garden station")
    rg_p.add_argument("url", help="radio.garden station URL")
    rg_p.add_argument("--interval", "-i", type=int, default=60,
                      help="Seconds between Shazam checks (default: 60)")
    rg_p.add_argument("--capture", "-c", type=int, default=30,
                      help="Seconds of audio to capture per check (default: 30)")
    rg_p.add_argument("--duration", "-d", type=int, default=0,
                      help="Total minutes to monitor (0 = run until Ctrl+C)")
    rg_p.add_argument("--cooldown", type=int, default=600,
                      help="Seconds before same track can be saved again (default: 600)")

    # mixcloud
    mc_p = sub.add_parser("mixcloud", help="Scan a Mixcloud mix and identify tracks")
    mc_p.add_argument("url", help="Mixcloud mix URL")
    mc_p.add_argument("--username", "-u", default=None, help="Mixcloud username")
    mc_p.add_argument("--password", "-p", default=None, help="Mixcloud password")
    mc_p.add_argument("--interval", "-i", type=int, default=60,
                      help="Seconds between Shazam checks (default: 60)")
    mc_p.add_argument("--capture", "-c", type=int, default=30,
                      help="Seconds of audio to capture per check (default: 30)")
    mc_p.add_argument("--output", "-o", default=None, help="Write tracklist to JSON file")
    mc_p.add_argument("--json", "-j", action="store_true", dest="json_output",
                      help="Print tracklist as JSON to stdout")

    # youtube
    yt_p = sub.add_parser("youtube", help="Scan a YouTube video and identify tracks")
    yt_p.add_argument("url", help="YouTube video URL")
    yt_p.add_argument("--interval", "-i", type=int, default=60,
                      help="Seconds between Shazam checks (default: 60)")
    yt_p.add_argument("--capture", "-c", type=int, default=30,
                      help="Seconds of audio to capture per check (default: 30)")
    yt_p.add_argument("--output", "-o", default=None, help="Write tracklist to JSON file")
    yt_p.add_argument("--json", "-j", action="store_true", dest="json_output",
                      help="Print tracklist as JSON to stdout")

    # podbean
    pb_p = sub.add_parser("podbean", help="Scan a Podbean episode and identify tracks")
    pb_p.add_argument("url", help="Podbean episode URL")
    pb_p.add_argument("--interval", "-i", type=int, default=60,
                      help="Seconds between Shazam checks (default: 60)")
    pb_p.add_argument("--capture", "-c", type=int, default=30,
                      help="Seconds of audio to capture per check (default: 30)")
    pb_p.add_argument("--output", "-o", default=None, help="Write tracklist to JSON file")
    pb_p.add_argument("--json", "-j", action="store_true", dest="json_output",
                      help="Print tracklist as JSON to stdout")

    # reddit
    rd_p = sub.add_parser("reddit", help="Extract tracks from a Reddit text post")
    rd_p.add_argument("url", help="Reddit post URL")

    # reddit-history
    rd_hist_p = sub.add_parser("reddit-history", help="Browse Reddit post scans")
    rd_hist_p.add_argument("-n", "--limit", type=int, default=20)

    # reddit-delete-session
    rd_del_p = sub.add_parser("reddit-delete-session", help="Delete a Reddit session and its tracks")
    rd_del_p.add_argument("session_id", type=int)
    rd_del_p.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")

    # history
    hist_p = sub.add_parser("history", help="Show all detected tracks from every source")
    hist_p.add_argument("-n", "--limit", type=int, default=20, help="Number of tracks to show")

    # instagram-history
    ig_hist_p = sub.add_parser("instagram-history", help="Browse detected Instagram posts and tracks")
    ig_hist_p.add_argument("-n", "--limit", type=int, default=20)
    ig_hist_p.add_argument("--tracks", "-t", action="store_true", dest="tracks_only",
                            help="Show flat track list instead of grouped by post")

    # radio-garden-history (alias: radio-history)
    rg_hist_p = sub.add_parser("radio-history", help="Browse radio.garden monitoring sessions")
    rg_hist_p.add_argument("-n", "--limit", type=int, default=10)

    # mixcloud-history
    mc_hist_p = sub.add_parser("mixcloud-history", help="Browse Mixcloud mix scans")
    mc_hist_p.add_argument("-n", "--limit", type=int, default=10)

    # mixcloud-delete-session
    mc_del_p = sub.add_parser("mixcloud-delete-session", help="Delete a Mixcloud session and its tracks")
    mc_del_p.add_argument("session_id", type=int)
    mc_del_p.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")

    # youtube-history
    yt_hist_p = sub.add_parser("youtube-history", help="Browse YouTube video scans")
    yt_hist_p.add_argument("-n", "--limit", type=int, default=10)

    # youtube-delete-session
    yt_del_p = sub.add_parser("youtube-delete-session", help="Delete a YouTube session and its tracks")
    yt_del_p.add_argument("session_id", type=int)
    yt_del_p.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")

    # podbean-history
    pb_hist_p = sub.add_parser("podbean-history", help="Browse Podbean episode scans")
    pb_hist_p.add_argument("-n", "--limit", type=int, default=10)

    # podbean-delete-session
    pb_del_p = sub.add_parser("podbean-delete-session", help="Delete a Podbean session and its tracks")
    pb_del_p.add_argument("session_id", type=int)
    pb_del_p.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")

    # login-instagram
    li_p = sub.add_parser("login-instagram", help="Save Instagram credentials and verify login")
    li_p.add_argument("--username", "-u", default=None, help="Instagram username")
    li_p.add_argument("--password", "-p", default=None, help="Instagram password")

    # login-mixcloud
    lm_p = sub.add_parser("login-mixcloud", help="Save Mixcloud credentials for future use")
    lm_p.add_argument("--username", "-u", default=None, help="Mixcloud username")
    lm_p.add_argument("--password", "-p", default=None, help="Mixcloud password")

    # enrich
    enrich_p = sub.add_parser(
        "enrich",
        help="Enrich detected tracks with Beatport metadata (bpm, key, genre, release_date)",
    )
    enrich_p.add_argument("--dry-run", action="store_true",
                          help="Show what would be enriched without writing to DB")
    enrich_p.add_argument("--limit", type=int, default=0, metavar="N",
                          help="Stop after N tracks (0 = no limit)")
    enrich_p.add_argument("--verbose", "-v", action="store_true",
                          help="Print Beatport search details")
    enrich_p.add_argument("--threshold", type=float, default=0.72, metavar="F",
                          help="Fuzzy match threshold 0-1 (default: 0.72)")
    enrich_p.add_argument("--retry-misses", "-r", action="store_true",
                          help="Retry tracks that previously had no results or fuzzy miss")

    # sync-beatport
    sb_p = sub.add_parser(
        "sync-beatport",
        help="Pull Beatport playlist tracks into enriched_tracks (incremental)",
    )
    sb_p.add_argument("--playlist", "-p", default=None, metavar="NAME",
                      help="Sync only this playlist (exact name). Omit to sync all.")
    sb_p.add_argument("--dry-run", action="store_true",
                      help="Show what would be added without writing")
    sb_p.add_argument("--verbose", "-v", action="store_true",
                      help="Print each track as it is added")
    sb_p.add_argument("--limit", type=int, default=0, metavar="N",
                      help="Stop after adding N new tracks (0 = no limit)")

    # enrich-studio
    es_p = sub.add_parser(
        "enrich-studio",
        help="Populate mik_key, mik_nrg, vocals, drums, melody from DJ Studio library (phase 2)",
    )
    es_p.add_argument("--dry-run", action="store_true",
                      help="Show what would be updated without writing to DB")
    es_p.add_argument("--limit", type=int, default=0, metavar="N",
                      help="Stop after N tracks (0 = no limit)")
    es_p.add_argument("--verbose", "-v", action="store_true",
                      help="Print per-track update details")
    es_p.add_argument("--test", action="store_true",
                      help="Operate on enriched_tracks_test instead of enriched_tracks")

    # import-to-studio
    its_p = sub.add_parser(
        "import-to-studio",
        help="Download Beatport previews → DJ Studio analysis (key/energy/cuepoints/beatgrid) → write DJ Studio library entries",
    )
    its_p.add_argument("--table", default="enriched_tracks_test",
                       help="Source table (default: enriched_tracks_test)")
    its_p.add_argument("--limit", type=int, default=0, metavar="N",
                       help="Stop after N tracks (0 = no limit)")
    its_p.add_argument("--keep-temp", action="store_true",
                       help="Keep the temp dir of downloaded preview MP3s")
    its_p.add_argument("--verbose", "-v", action="store_true")
    its_p.add_argument("--seed", type=int, default=0, metavar="N",
                       help="(Re)create enriched_tracks_test with the N most-recently-enriched rows first")

    # sessions
    _TYPES = ("youtube", "instagram", "mixcloud", "radio", "podbean", "reddit")
    sess_p = sub.add_parser("sessions", help="List all sessions for a source type")
    sess_p.add_argument("type", choices=_TYPES, metavar="TYPE",
                        help=f"Source type: {', '.join(_TYPES)}")
    sess_p.add_argument("-n", "--limit", type=int, default=20)

    # enriched
    enriched_p = sub.add_parser(
        "enriched",
        help="List all enriched tracks, newest first",
    )
    enriched_p.add_argument("-n", "--limit", type=int, default=50,
                            help="Max rows to show (default: 50)")
    enriched_p.add_argument("--playlist", "-p", default=None, metavar="NAME",
                            help="Filter to tracks from a specific Beatport playlist")

    # enrich-runs
    eh_p = sub.add_parser(
        "enrich-runs",
        help="Show past enrich run summaries",
    )
    eh_p.add_argument("-n", "--limit", type=int, default=20,
                      help="Max runs to show (default: 20)")

    # enrich-tracks
    st_p = sub.add_parser(
        "enrich-tracks",
        help="Show all tracks for a session with enrichment data (fuzzy_miss flagged with ~)",
    )
    st_p.add_argument("type", choices=_TYPES, metavar="TYPE",
                      help=f"Source type: {', '.join(_TYPES)}")  # _TYPES defined above
    st_p.add_argument("session_id", type=int)

    return detect_p


def dispatch(args, detect_p: argparse.ArgumentParser) -> None:
    """Dispatch a parsed `dj detect ...` invocation."""
    import os
    migrate()

    if not args.detect_command:
        detect_p.print_help()
        return

    cmd = args.detect_command

    if cmd == "reddit":
        url = args.url
        prior = find_session(url)
        if prior:
            n_tracks = len(tracks_for_session(prior["id"]))
            console.print(
                f"\n[dim]This post was already scanned (session #{prior['id']}, "
                f"{n_tracks} track(s) found).[/dim]\n"
            )
            if not _confirm("Scan again?", default=False):
                sys.exit(0)

        # Extract subreddit from URL for display
        sr_m = __import__("re").search(r"/r/([^/?#]+)", url)
        subreddit = sr_m.group(1) if sr_m else "reddit"

        console.print(
            f"\n[bold]Paste the post body into vi, then save and quit (:wq).[/bold]\n"
            f"[dim]URL: {url}[/dim]\n"
        )
        raw_text = reddit_open_editor(url)

        tracks = reddit_extract_from_text(raw_text)
        if not tracks:
            console.print("[yellow]No tracks found — nothing saved.[/yellow]")
            sys.exit(0)

        console.print(f"\n[bold]Found {len(tracks)} track(s) from r/{subreddit}:[/bold]\n")
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("#",      style="dim", width=4)
        t.add_column("Artist", min_width=22)
        t.add_column("Title",  min_width=28)
        for tr in tracks:
            t.add_row(str(tr["position"]), tr["artist"], tr["title"])
        console.print(t)

        # Derive a title from the URL slug
        slug = url.rstrip("/").split("/")[-1].replace("_", " ").title()
        session_id = create_session(
            "reddit", url, slug,
            uploader=subreddit,
        )
        insert_tracks(tracks, source="reddit", session_id=session_id)
        end_session(session_id)
        console.print(f"\n[dim]Saved to DB (session #{session_id})[/dim]")

    elif cmd == "reddit-history":
        sessions = list_sessions("reddit", args.limit)
        if not sessions:
            console.print("[dim]No Reddit sessions yet.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("ID",         style="dim", width=5)
        t.add_column("Title",      min_width=40)
        t.add_column("Subreddit",  min_width=16)
        t.add_column("Scanned",    style="dim", width=19)
        t.add_column("Tracks",     style="dim", width=7)
        for r in sessions:
            t.add_row(str(r["id"]), r["title"] or "—", r["uploader"] or "—",
                      r["started_at"][:19], str(r["track_count"]))
        console.print(t)

    elif cmd == "reddit-delete-session":
        session_id = args.session_id
        rows = tracks_for_session(session_id)
        if not rows:
            console.print(f"[yellow]Session #{session_id} not found.[/yellow]")
            sys.exit(1)
        if not args.force:
            console.print(f"[yellow]Delete Reddit session #{session_id} ({len(rows)} track(s))?[/yellow]")
            if not _confirm("Confirm delete", default=False):
                sys.exit(0)
        n = delete_session(session_id)
        console.print(f"[green]Deleted session #{session_id}.[/green]")

    elif cmd == "instagram":
        saved_u, saved_p = _load_saved_credentials(service="instagram")
        username = args.username or os.environ.get("IG_USERNAME") or saved_u
        password = args.password or os.environ.get("IG_PASSWORD") or saved_p
        if not username:
            username = input("Instagram username: ")
        if not password:
            password = getpass.getpass("Instagram password: ")
        asyncio.run(_run(args.url, username, password, args.output, args.json_output))

    elif cmd == "radio-garden":
        if args.capture >= args.interval:
            console.print(f"[red]--capture ({args.capture}s) must be shorter than --interval ({args.interval}s)[/red]")
            sys.exit(1)
        asyncio.run(_run_radio(args.url, interval=args.interval, capture_s=args.capture,
                               duration_min=args.duration, cooldown=args.cooldown))

    elif cmd == "mixcloud":
        if args.capture >= args.interval:
            console.print(f"[red]--capture ({args.capture}s) must be shorter than --interval ({args.interval}s)[/red]")
            sys.exit(1)
        saved_u, saved_p = _load_saved_credentials(service="mixcloud")
        username = args.username or os.environ.get("MC_USERNAME") or saved_u
        password = args.password or os.environ.get("MC_PASSWORD") or saved_p

        resume_session_id: int | None = None
        resume_from: int = 0
        prior = find_session(args.url)
        if prior:
            n_tracks = len(tracks_for_session(prior["id"]))
            last_pos = prior["last_scanned_position"]
            dur = prior["duration_seconds"] or 0
            if last_pos is None and n_tracks:
                last_pos = infer_last_position(prior["id"])
            is_partial = last_pos is not None and (not dur or last_pos < dur - args.interval)
            if is_partial:
                note = " (position inferred from tracks)" if prior["last_scanned_position"] is None else ""
                console.print(
                    f"\n[yellow]Found an incomplete session (#{prior['id']}) for this mix.[/yellow]\n"
                    f"  Last scanned: [bold]{_fmt_time(last_pos)}[/bold]{note}  ·  {n_tracks} track(s) found so far\n"
                )
                if _confirm("Resume from where it left off?", default=True):
                    resume_session_id = prior["id"]
                    resume_from = last_pos
                    if prior["last_scanned_position"] is None:
                        update_session_progress(prior["id"], last_pos)
                else:
                    console.print("")
            else:
                scanned_on = prior["started_at"][:10]
                console.print(
                    f"\n[dim]This mix was already scanned on {scanned_on} "
                    f"({n_tracks} track(s) found, session #{prior['id']}).[/dim]\n"
                )
                if not _confirm("Scan again from the beginning?", default=False):
                    sys.exit(0)
                console.print("")

        asyncio.run(_run_mixcloud(
            args.url, username, password, args.interval, args.capture,
            args.output, args.json_output,
            resume_session_id=resume_session_id, resume_from=resume_from,
        ))

    elif cmd == "youtube":
        if args.capture >= args.interval:
            console.print(f"[red]--capture ({args.capture}s) must be shorter than --interval ({args.interval}s)[/red]")
            sys.exit(1)

        resume_session_id = None
        resume_from = 0
        prior = find_session(args.url)
        if prior:
            n_tracks = len(tracks_for_session(prior["id"]))
            last_pos = prior["last_scanned_position"]
            dur = prior["duration_seconds"] or 0
            if last_pos is None and n_tracks:
                last_pos = infer_last_position(prior["id"])
            is_partial = last_pos is not None and (not dur or last_pos < dur - args.interval)
            if is_partial:
                note = " (position inferred from tracks)" if prior["last_scanned_position"] is None else ""
                console.print(
                    f"\n[yellow]Found an incomplete session (#{prior['id']}) for this video.[/yellow]\n"
                    f"  Last scanned: [bold]{_fmt_time(last_pos)}[/bold]{note}  ·  {n_tracks} track(s) found so far\n"
                )
                if _confirm("Resume from where it left off?", default=True):
                    resume_session_id = prior["id"]
                    resume_from = last_pos
                    if prior["last_scanned_position"] is None:
                        update_session_progress(prior["id"], last_pos)
                else:
                    console.print("")
            else:
                scanned_on = prior["started_at"][:10]
                console.print(
                    f"\n[dim]This video was already scanned on {scanned_on} "
                    f"({n_tracks} track(s) found, session #{prior['id']}).[/dim]\n"
                )
                if not _confirm("Scan again from the beginning?", default=False):
                    sys.exit(0)
                console.print("")

        asyncio.run(_run_youtube(args.url, args.interval, args.capture, args.output, args.json_output,
                                 resume_session_id=resume_session_id, resume_from=resume_from))

    elif cmd == "podbean":
        if args.capture >= args.interval:
            console.print(f"[red]--capture ({args.capture}s) must be shorter than --interval ({args.interval}s)[/red]")
            sys.exit(1)

        resume_session_id = None
        resume_from = 0
        prior = find_session(args.url)
        if prior:
            n_tracks = len(tracks_for_session(prior["id"]))
            last_pos = prior["last_scanned_position"]
            dur = prior["duration_seconds"] or 0
            if last_pos is None and n_tracks:
                last_pos = infer_last_position(prior["id"])
            is_partial = last_pos is not None and (not dur or last_pos < dur - args.interval)
            if is_partial:
                note = " (position inferred from tracks)" if prior["last_scanned_position"] is None else ""
                console.print(
                    f"\n[yellow]Found an incomplete session (#{prior['id']}) for this episode.[/yellow]\n"
                    f"  Last scanned: [bold]{_fmt_time(last_pos)}[/bold]{note}  ·  {n_tracks} track(s) found so far\n"
                )
                if _confirm("Resume from where it left off?", default=True):
                    resume_session_id = prior["id"]
                    resume_from = last_pos
                    if prior["last_scanned_position"] is None:
                        update_session_progress(prior["id"], last_pos)
                else:
                    console.print("")
            else:
                scanned_on = prior["started_at"][:10]
                console.print(
                    f"\n[dim]This episode was already scanned on {scanned_on} "
                    f"({n_tracks} track(s) found, session #{prior['id']}).[/dim]\n"
                )
                if not _confirm("Scan again from the beginning?", default=False):
                    sys.exit(0)
                console.print("")

        asyncio.run(_run_podbean(args.url, args.interval, args.capture, args.output, args.json_output,
                                 resume_session_id=resume_session_id, resume_from=resume_from))

    elif cmd == "history":
        rows = list_tracks(args.limit)
        if not rows:
            console.print("[dim]No tracks stored yet.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("ID", style="dim", width=5)
        t.add_column("Source", style="dim", width=12)
        t.add_column("Artist", min_width=22)
        t.add_column("Title", min_width=28)
        t.add_column("Apple Music", min_width=40)
        t.add_column("Detected", style="dim", min_width=20)
        for r in rows:
            t.add_row(
                str(r["id"]), r["source"] or "—", r["artist"] or "—", r["title"] or "—",
                r["apple_music_url"] or "—", r["synced_at"][:19],
            )
        console.print(t)

    elif cmd == "instagram-history":
        if args.tracks_only:
            rows = [r for r in list_tracks(args.limit) if (r["source"] or "") == "instagram"]
            if not rows:
                console.print("[dim]No Instagram tracks stored yet.[/dim]")
                return
            t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
            t.add_column("ID", style="dim", width=5)
            t.add_column("Artist", min_width=22)
            t.add_column("Title", min_width=28)
            t.add_column("Apple Music", min_width=40)
            t.add_column("Detected", style="dim", min_width=20)
            for r in rows:
                t.add_row(str(r["id"]), r["artist"] or "—", r["title"] or "—",
                          r["apple_music_url"] or "—", r["synced_at"][:19])
            console.print(t)
            return

        sessions = list_sessions("instagram", args.limit)
        if not sessions:
            console.print("[dim]No Instagram posts stored yet.[/dim]")
            return
        for s in sessions:
            caption_preview = (s["caption"] or "")[:80].replace("\n", " ")
            console.print(
                f"\n[bold cyan]#{s['id']}[/bold cyan]  {s['url']}\n"
                f"  [dim]detected:[/dim] {s['started_at'][:19]}\n"
                f"  [dim]{caption_preview}{'…' if len(s['caption'] or '') > 80 else ''}[/dim]"
            )
            tracks = tracks_for_session(s["id"])
            if tracks:
                t = Table(show_header=False, box=None, padding=(0, 3))
                t.add_column("#", style="dim", width=4)
                t.add_column("Artist", min_width=20)
                t.add_column("Title", min_width=25)
                t.add_column("Apple Music", min_width=38)
                for r in tracks:
                    t.add_row(str(r["position"] or ""), r["artist"] or "—",
                              r["title"] or "—", r["apple_music_url"] or "—")
                console.print(t)

    elif cmd == "radio-history":
        rows = list_sessions("radio", args.limit)
        if not rows:
            console.print("[dim]No radio sessions yet. Run [bold]dj detect radio-garden <url>[/bold] to start.[/dim]")
            return
        for s in rows:
            ended = s["ended_at"][:19] if s["ended_at"] else "ongoing"
            console.print(
                f"\n[bold cyan]Session #{s['id']}[/bold cyan]  [bold]{s['title']}[/bold]\n"
                f"  [dim]started:[/dim] {s['started_at'][:19]}  [dim]ended:[/dim] {ended}\n"
                f"  [dim]{s['url']}[/dim]"
            )
            tracks = tracks_for_session(s["id"])
            if tracks:
                t = Table(show_header=False, box=None, padding=(0, 3))
                t.add_column("Time", style="dim", min_width=20)
                t.add_column("Artist", min_width=22)
                t.add_column("Title", min_width=28)
                t.add_column("Apple Music", min_width=38)
                for r in tracks:
                    t.add_row(r["synced_at"][:19], r["artist"] or "—", r["title"] or "—",
                              r["apple_music_url"] or "—")
                console.print(t)
            else:
                console.print("  [dim](no tracks detected)[/dim]")

    elif cmd == "mixcloud-history":
        rows = list_sessions("mixcloud", args.limit)
        if not rows:
            console.print("[dim]No Mixcloud sessions yet. Run [bold]dj detect mixcloud <url>[/bold] to start.[/dim]")
            return
        for s in rows:
            ended = s["ended_at"][:19] if s["ended_at"] else "ongoing"
            dur = f"  [dim]duration:[/dim] {_fmt_time(s['duration_seconds'])}" if s["duration_seconds"] else ""
            console.print(
                f"\n[bold cyan]Session #{s['id']}[/bold cyan]  [bold]{s['title']}[/bold]\n"
                f"  [dim]uploader:[/dim] {s['uploader'] or '?'}  "
                f"[dim]scanned:[/dim] {s['started_at'][:19]}  [dim]ended:[/dim] {ended}{dur}"
            )
            tracks = tracks_for_session(s["id"])
            if tracks:
                t = Table(show_header=False, box=None, padding=(0, 3))
                t.add_column("Time", style="dim", width=8)
                t.add_column("Artist", min_width=22)
                t.add_column("Title", min_width=28)
                t.add_column("Apple Music", min_width=38)
                for r in tracks:
                    pos = r["position"]
                    t.add_row(_fmt_time(pos) if isinstance(pos, int) else "—",
                              r["artist"] or "—", r["title"] or "—", r["apple_music_url"] or "—")
                console.print(t)
            else:
                console.print("  [dim](no tracks detected)[/dim]")

    elif cmd == "mixcloud-delete-session":
        rows = list_sessions("mixcloud", 100)
        session = next((r for r in rows if r["id"] == args.session_id), None)
        if not session:
            console.print(f"[red]Session #{args.session_id} not found.[/red]")
            sys.exit(1)
        n_tracks = len(tracks_for_session(args.session_id))
        console.print(
            f"Session #{args.session_id}: [bold]{session['title']}[/bold]  "
            f"({n_tracks} track(s), scanned {session['started_at'][:10]})"
        )
        if not args.force and not _confirm("Delete this session and its tracks?", default=False):
            sys.exit(0)
        delete_session(args.session_id)
        console.print(f"[green]✓[/green] Deleted session #{args.session_id} and {n_tracks} track(s).")

    elif cmd == "youtube-history":
        rows = list_sessions("youtube", args.limit)
        if not rows:
            console.print("[dim]No YouTube sessions yet. Run [bold]dj detect youtube <url>[/bold] to start.[/dim]")
            return
        for s in rows:
            ended = s["ended_at"][:19] if s["ended_at"] else "ongoing"
            dur = f"  [dim]duration:[/dim] {_fmt_time(s['duration_seconds'])}" if s["duration_seconds"] else ""
            console.print(
                f"\n[bold cyan]Session #{s['id']}[/bold cyan]  [bold]{s['title']}[/bold]\n"
                f"  [dim]uploader:[/dim] {s['uploader'] or '?'}  "
                f"[dim]scanned:[/dim] {s['started_at'][:19]}  [dim]ended:[/dim] {ended}{dur}"
            )
            tracks = tracks_for_session(s["id"])
            if tracks:
                t = Table(show_header=False, box=None, padding=(0, 3))
                t.add_column("Time", style="dim", width=8)
                t.add_column("Artist", min_width=22)
                t.add_column("Title", min_width=28)
                t.add_column("Apple Music", min_width=38)
                for r in tracks:
                    pos = r["position"]
                    t.add_row(_fmt_time(pos) if isinstance(pos, int) else "—",
                              r["artist"] or "—", r["title"] or "—", r["apple_music_url"] or "—")
                console.print(t)
            else:
                console.print("  [dim](no tracks detected)[/dim]")

    elif cmd == "youtube-delete-session":
        rows = list_sessions("youtube", 100)
        session = next((r for r in rows if r["id"] == args.session_id), None)
        if not session:
            console.print(f"[red]Session #{args.session_id} not found.[/red]")
            sys.exit(1)
        n_tracks = len(tracks_for_session(args.session_id))
        console.print(
            f"Session #{args.session_id}: [bold]{session['title']}[/bold]  "
            f"({n_tracks} track(s), scanned {session['started_at'][:10]})"
        )
        if not args.force and not _confirm("Delete this session and its tracks?", default=False):
            sys.exit(0)
        delete_session(args.session_id)
        console.print(f"[green]✓[/green] Deleted session #{args.session_id} and {n_tracks} track(s).")

    elif cmd == "podbean-history":
        rows = list_sessions("podbean", args.limit)
        if not rows:
            console.print("[dim]No Podbean sessions yet. Run [bold]dj detect podbean <url>[/bold] to start.[/dim]")
            return
        for s in rows:
            ended = s["ended_at"][:19] if s["ended_at"] else "ongoing"
            dur = f"  [dim]duration:[/dim] {_fmt_time(s['duration_seconds'])}" if s["duration_seconds"] else ""
            console.print(
                f"\n[bold cyan]Session #{s['id']}[/bold cyan]  [bold]{s['title']}[/bold]\n"
                f"  [dim]uploader:[/dim] {s['uploader'] or '?'}  "
                f"[dim]scanned:[/dim] {s['started_at'][:19]}  [dim]ended:[/dim] {ended}{dur}"
            )
            tracks = tracks_for_session(s["id"])
            if tracks:
                t = Table(show_header=False, box=None, padding=(0, 3))
                t.add_column("Time", style="dim", width=8)
                t.add_column("Artist", min_width=22)
                t.add_column("Title", min_width=28)
                t.add_column("Apple Music", min_width=38)
                for r in tracks:
                    pos = r["position"]
                    t.add_row(_fmt_time(pos) if isinstance(pos, int) else "—",
                              r["artist"] or "—", r["title"] or "—", r["apple_music_url"] or "—")
                console.print(t)
            else:
                console.print("  [dim](no tracks detected)[/dim]")

    elif cmd == "podbean-delete-session":
        rows = list_sessions("podbean", 100)
        session = next((r for r in rows if r["id"] == args.session_id), None)
        if not session:
            console.print(f"[red]Session #{args.session_id} not found.[/red]")
            sys.exit(1)
        n_tracks = len(tracks_for_session(args.session_id))
        console.print(
            f"Session #{args.session_id}: [bold]{session['title']}[/bold]  "
            f"({n_tracks} track(s), scanned {session['started_at'][:10]})"
        )
        if not args.force and not _confirm("Delete this session and its tracks?", default=False):
            sys.exit(0)
        delete_session(args.session_id)
        console.print(f"[green]✓[/green] Deleted session #{args.session_id} and {n_tracks} track(s).")

    elif cmd == "login-instagram":
        username = args.username or os.environ.get("IG_USERNAME") or input("Instagram username: ")
        password = args.password or os.environ.get("IG_PASSWORD") or getpass.getpass("Instagram password: ")
        console.print("[dim]Logging into Instagram…[/dim]")
        try:
            build_client(username, password,
                         challenge_handler=_challenge_handler,
                         two_factor_handler=_two_factor_handler)
        except Exception as exc:
            console.print(f"[red]Login failed:[/red] {exc}")
            sys.exit(1)
        _save_credentials(username, password, service="instagram")
        console.print(f"[green]✓[/green] Logged in. Credentials saved to {CONFIG_FILE}")

    elif cmd == "login-mixcloud":
        username = args.username or os.environ.get("MC_USERNAME") or input("Mixcloud username: ")
        password = args.password or os.environ.get("MC_PASSWORD") or getpass.getpass("Mixcloud password: ")
        _save_credentials(username, password, service="mixcloud")
        console.print(f"[green]✓[/green] Mixcloud credentials saved to {CONFIG_FILE}")

    elif cmd == "enrich":
        from detect.enrich import run_enrich
        run_enrich(
            dry_run=args.dry_run,
            limit=args.limit,
            verbose=args.verbose,
            threshold=args.threshold,
            retry_misses=args.retry_misses,
        )

    elif cmd == "sync-beatport":
        from detect.sync_beatport import run_sync_beatport
        run_sync_beatport(dry_run=args.dry_run, verbose=args.verbose, limit=args.limit, playlist=args.playlist)

    elif cmd == "enrich-studio":
        from detect.enrich_studio import run_enrich_studio
        table = "enriched_tracks_test" if getattr(args, "test", False) else "enriched_tracks"
        run_enrich_studio(dry_run=args.dry_run, limit=args.limit, verbose=args.verbose, table=table)

    elif cmd == "import-to-studio":
        from detect.import_to_studio import run_import_to_studio
        if args.seed:
            n = detect_db.create_enriched_tracks_test(limit=args.seed)
            console.print(f"[green]✓[/green] Seeded enriched_tracks_test with {n} rows")
        run_import_to_studio(
            table=args.table,
            limit=args.limit,
            keep_temp=args.keep_temp,
            verbose=args.verbose,
        )

    elif cmd == "sessions":
        rows = list_sessions(args.type, args.limit)
        if not rows:
            console.print(f"[dim]No {args.type} sessions yet.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        if args.type == "instagram":
            t.add_column("ID", style="dim", width=5)
            t.add_column("URL", min_width=40)
            t.add_column("Detected", style="dim", min_width=19)
            t.add_column("Tracks", style="dim", width=7)
            for r in rows:
                t.add_row(str(r["id"]), r["url"], r["started_at"][:19], str(r["track_count"]))
        else:
            extra_label = {"radio": "Station", "mixcloud": "Uploader",
                           "youtube": "Uploader", "podbean": "Podcast",
                           "reddit": "Subreddit"}[args.type]
            t.add_column("ID", style="dim", width=5)
            t.add_column("Title", min_width=32)
            t.add_column(extra_label, min_width=18)
            t.add_column("Scanned", style="dim", min_width=19)
            t.add_column("Tracks", style="dim", width=7)
            for r in rows:
                t.add_row(str(r["id"]), r["title"] or "—", r["uploader"] or "—",
                          r["started_at"][:19], str(r["track_count"]))
        console.print(t)

    elif cmd == "enriched":
        from .db import list_enriched_tracks
        rows = list_enriched_tracks(args.limit, playlist_name=getattr(args, "playlist", None))
        if not rows:
            console.print("[dim]No enriched tracks yet.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("Artist",      min_width=20)
        t.add_column("Title",       min_width=24)
        t.add_column("BPM",         style="dim", width=6)
        t.add_column("Key",         style="dim", width=5)
        t.add_column("Genre",       min_width=14)
        t.add_column("Released",    style="dim", width=11)
        t.add_column("BP ID",       style="dim", width=10)
        t.add_column("Link",        style="blue", no_wrap=True)
        t.add_column("Apple Music", style="dim", no_wrap=True)
        t.add_column("MIK Key",     style="dim", width=8)
        t.add_column("Nrg",         style="dim", width=4)
        t.add_column("Stems",       style="dim", width=16)
        for r in rows:
            bpm      = f"{r['bpm']:.0f}" if r["bpm"] else "—"
            released = (r["release_date"] or "—")[:10]
            bp_id    = str(r["beatport_id"]) if r["beatport_id"] else "—"
            bp_link  = r["beatport_link"] or "—"
            am_link  = r["apple_music_url"] or "—"
            mik_key  = r["mik_key"] or "—"
            mik_nrg  = str(r["mik_nrg"]) if r["mik_nrg"] is not None else "—"
            stems_parts = [
                f"V:{r['vocals'][0].upper()}" if r["vocals"] else "",
                f"D:{r['drums'][0].upper()}"  if r["drums"]  else "",
                f"M:{r['melody'][0].upper()}" if r["melody"] else "",
            ]
            stems = " ".join(p for p in stems_parts if p) or "—"
            t.add_row(r["artist"] or "—", r["title"] or "—",
                      bpm, r["key"] or "—", r["genre"] or "—",
                      released, bp_id, bp_link, am_link, mik_key, mik_nrg, stems)
        console.print(t)
        console.print(f"\n[dim]{len(rows)} enriched tracks[/dim]")

    elif cmd == "enrich-runs":
        from .db import list_enrich_runs
        runs = list_enrich_runs(args.limit)
        if not runs:
            console.print("[dim]No enrich runs yet.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("ID",         style="dim", width=5)
        t.add_column("Started",    style="dim", width=20)
        t.add_column("Finished",   style="dim", width=20)
        t.add_column("Seen",       style="dim", width=6)
        t.add_column("Enriched",   style="green", width=9)
        t.add_column("No results", style="yellow", width=11)
        t.add_column("Fuzzy miss", style="yellow", width=11)
        t.add_column("Status",     style="dim", width=8)
        for r in runs:
            t.add_row(
                str(r["id"]),
                r["started_at"][:19],
                (r["finished_at"] or "—")[:19],
                str(r["seen"]),
                str(r["found"]),
                str(r["not_found"]),
                str(r["fuzzy_miss"]),
                r["status"] or "—",
            )
        console.print(t)
        console.print(f"\n[dim]{len(runs)} runs[/dim]")

    elif cmd == "enrich-tracks":
        session_type = getattr(args, "type", None)

        def _pos_str(pos) -> str:
            if pos is None:
                return "—"
            return f"#{pos}" if session_type in ("instagram", "reddit") else _fmt_time(pos)

        rows = tracks_for_session_enriched(args.session_id)
        if not rows:
            console.print(f"[dim]No tracks for {args.type} session #{args.session_id}.[/dim]")
            return
        t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        t.add_column("#",        style="dim", width=8)
        t.add_column("Artist",   min_width=20)
        t.add_column("Title",    min_width=24)
        t.add_column("BPM",      style="dim", width=6)
        t.add_column("Key",      style="dim", width=5)
        t.add_column("Genre",    min_width=14)
        t.add_column("Released", style="dim", width=11)
        t.add_column("BP ID",    style="dim", width=10)
        t.add_column("Link",     style="blue", no_wrap=True)
        t.add_column("MIK Key",  style="dim", width=8)
        t.add_column("Nrg",      style="dim", width=4)
        for r in rows:
            outcome  = r["enrich_outcome"] or ""
            fuzzy    = "[yellow]~[/yellow]" if outcome == "fuzzy_miss" else ""
            artist   = (r["artist"] or "—") + (f" {fuzzy}" if fuzzy else "")
            bpm      = f"{r['bpm']:.0f}" if r["bpm"] else "—"
            released = (r["release_date"] or "—")[:10]
            bp_id    = str(r["beatport_id"]) if r["beatport_id"] else "—"
            bp_link  = r["beatport_link"] or "—"
            mik_key  = r["mik_key"] or "—"
            mik_nrg  = str(r["mik_nrg"]) if r["mik_nrg"] is not None else "—"
            t.add_row(_pos_str(r["position"]), artist, r["title"] or "—",
                      bpm, r["key"] or "—", r["genre"] or "—",
                      released, bp_id, bp_link, mik_key, mik_nrg)
        console.print(t)
        console.print(f"\n[dim]{len(rows)} tracks[/dim]")
