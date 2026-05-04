"""Push tracks from `enriched_tracks_test` (or any compatible table) into a
named rekordbox playlist as Beatport streaming entries.

User then opens rekordbox manually and runs `Track → Analyze Track` (or has
auto-analyse enabled) to produce ANLZ files containing PSSI phrase tags. A
later reader (TODO) will pull those tags back into our DB so we get rekordbox-
quality phrase labels (Intro / Verse / Pre-Chorus / Chorus / Bridge / Outro,
or Mood-3 EDM variant).

Reuses the same DjmdContent + playlist-creation primitives as
`rekordbox/importer.py` (the export-studio pipeline).

Constraint: rekordbox MUST be quit before we write to its database (it locks
master.db while running). Pre-flight check via psutil aborts with a clear
message.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Optional
from uuid import uuid4

import psutil
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from rekordbox.backup import backup_db

console = Console()


def is_rekordbox_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            n = (proc.info.get("name") or "").lower()
            if n == "rekordbox" or n.startswith("rekordbox "):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _split_title(title: str) -> tuple[str, str]:
    """'Like You Do (Original Mix) 05:27' → ('Like You Do', 'Original Mix').

    Pull off the last (...) block as Subtitle, drop trailing 'MM:SS' duration
    artifacts our table has appended in some rows.
    """
    t = title.strip()
    # Strip trailing MM:SS we sometimes have appended.
    if len(t) > 6 and t[-3] == ":" and t[-6] == " " and t[-2:].isdigit() and t[-5:-3].isdigit():
        t = t[:-6].rstrip()
    subtitle = ""
    if "(" in t and t.endswith(")"):
        idx = t.rfind("(")
        subtitle = t[idx + 1 : -1].strip()
        t = t[:idx].strip()
    return t, subtitle


def export_to_rekordbox(
    *,
    table: str = "enriched_tracks_test",
    playlist_name: str = "DJ Tools - Enrich",
    limit: int = 0,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    if is_rekordbox_running():
        console.print(
            "[red]rekordbox is currently running.[/red]\n"
            "Quit rekordbox before running this command — it locks master.db while open."
        )
        return

    # Lazy import to avoid hard dependency when only the detect pipeline is used.
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    # Use the shared db helper so skip rule (rekordbox_export_at IS NULL) +
    # forward-compat schema migrations stay in one place.
    from detect import db as detect_db

    detect_db.migrate()  # ensures rekordbox_export_at column exists
    pending = detect_db.get_export_to_rekordbox_pending(table=table, force=force)
    rows = [dict(r) for r in pending]
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print(
            f"Nothing to export — every track in {table} already has "
            "rekordbox_export_at set.\n[dim]Use --force to re-push all tracks.[/dim]"
        )
        return

    console.print(f"[bold]export-to-rekordbox[/bold] ← {len(rows)} tracks from [cyan]{table}[/cyan]")
    console.print(f"  playlist: [yellow]{playlist_name}[/yellow]")
    if dry_run:
        console.print("  [dim]DRY RUN — no writes to rekordbox[/dim]")

    # Open + back up rekordbox DB
    db = Rekordbox6Database()
    try:
        device = db.get_device().first()
        if device is None:
            console.print("[red]Could not find rekordbox device — is rekordbox set up?[/red]")
            return

        if not dry_run:
            backup = backup_db(playlist_name)
            if backup is None:
                console.print("[red]Backup failed — aborting.[/red]")
                return
            console.print(f"[dim]Backed up master.db → {backup}[/dim]")

        # Create or reuse the playlist
        existing_pl = db.get_playlist(Name=playlist_name).first()
        if existing_pl is not None:
            console.print(f"[dim]Reusing existing playlist (id={existing_pl.ID})[/dim]")
            playlist = existing_pl
        else:
            if dry_run:
                console.print("[dim]Would create new playlist[/dim]")
                playlist = None
            else:
                playlist = db.create_playlist(name=playlist_name)
                console.print(f"[dim]Created playlist (id={playlist.ID})[/dim]")

        existing_track_ids: set[str] = set()
        if playlist is not None and not dry_run:
            existing_songs = db.get_playlist_songs(PlaylistID=playlist.ID).all()
            existing_track_ids = {str(s.ContentID) for s in existing_songs}

        progress = Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TaskProgressColumn(),
            console=console,
        )

        counts = {"new": 0, "existing_track": 0, "added_to_playlist": 0,
                  "already_in_playlist": 0, "skipped": 0}

        with progress:
            t = progress.add_task("Adding tracks…", total=len(rows))
            for pos, row in enumerate(rows, 1):
                progress.update(
                    t, advance=1,
                    description=f"{row['artist']} — {row['title']}"[:60],
                )

                bid = row["beatport_id"]
                if not bid:
                    counts["skipped"] += 1
                    continue

                folder_path = f"/v4/catalog/tracks/{bid}/"
                content = db.get_content(FolderPath=folder_path).first()

                if content is None:
                    counts["new"] += 1
                    if dry_run:
                        continue
                    content = _create_beatport_content(
                        db, tables, row, folder_path, device,
                    )
                else:
                    counts["existing_track"] += 1

                if playlist is None:  # dry-run with new playlist
                    counts["added_to_playlist"] += 1
                    continue

                if str(content.ID) in existing_track_ids:
                    counts["already_in_playlist"] += 1
                    if not dry_run:
                        detect_db.mark_pipeline_done(table, bid, "rekordbox_export_at")
                    continue

                if not dry_run:
                    db.add_to_playlist(playlist=playlist, content=content, track_no=pos)
                    detect_db.mark_pipeline_done(table, bid, "rekordbox_export_at")
                counts["added_to_playlist"] += 1

        if not dry_run:
            db.commit()

        console.print()
        console.print(f"[bold]Done.[/bold]")
        console.print(f"  new tracks created:        {counts['new']}")
        console.print(f"  existing tracks reused:    {counts['existing_track']}")
        console.print(f"  added to playlist:         {counts['added_to_playlist']}")
        console.print(f"  already in playlist:       {counts['already_in_playlist']}")
        console.print(f"  skipped (no beatport_id):  {counts['skipped']}")
        console.print()
        console.print(
            "[dim]Next:[/dim] open rekordbox, find the playlist, right-click → "
            "[cyan]Analyze Tracks[/cyan]. Once analyzed, the ANLZ files will "
            "contain PSSI phrase tags we can read back later."
        )
    finally:
        db.close()


def _create_beatport_content(db, tables, row, folder_path, device):
    content_id = str(db.generate_unused_id(tables.DjmdContent))
    content_uuid = str(uuid4())

    artist_name = row.get("artist") or "Unknown"
    artist = db.get_artist(Name=artist_name).first() or db.add_artist(name=artist_name)

    genre_name = row.get("genre") or ""
    genre_id = None
    if genre_name:
        genre = db.get_genre(Name=genre_name).first() or db.add_genre(name=genre_name)
        genre_id = genre.ID

    key_id = None
    key = row.get("key")
    if key and key != "N/A":
        key_obj = db.get_key(ScaleName=key).first()
        if key_obj:
            key_id = key_obj.ID

    bpm_raw = row.get("bpm") or 0
    bpm = int(bpm_raw * 100) if bpm_raw else 0

    length = int(row.get("duration_sec") or 0)
    title, subtitle = _split_title(row.get("title") or "Unknown")

    today = str(datetime.date.today())

    content = tables.DjmdContent.create(
        ID=content_id, UUID=content_uuid,
        FolderPath=folder_path, FileNameL=folder_path, FileNameS="",
        Title=title, Subtitle=subtitle,
        ArtistID=artist.ID, OrgArtistID=artist.ID,
        GenreID=genre_id, KeyID=key_id,
        BPM=bpm, Length=length,
        FileType=20,  # 20 = Beatport streaming
        FileSize=0, BitRate=0, BitDepth=16, SampleRate=44100,
        Rating=0, Commnt="", ColorID="0",
        StockDate=today, DateCreated="",
        Analysed=0,
        DJPlayCount=0, TrackNo=0, DiscNo=0,
        DeviceID=device.ID, MasterDBID=device.MasterDBID, MasterSongID=content_id,
        AnalysisDataPath=f"/PIONEER/USBANLZ/{content_uuid[:1].lower()}/{content_uuid[1:3].lower()}/{content_uuid}",
        rb_file_id="0",
        HotCueAutoLoad="on", DeliveryControl="on", DeliveryComment="", ContentLink=0,
        ExtInfo='{"StreamingInfo": {"AudioQuality": "0", "AudioQualityWhenAnalyzed": "0"}}',
    )
    db.add(content)
    db.flush()
    return content
