"""Push tracks from `enriched_tracks_test` (or production) into a named rekordbox
playlist as Beatport streaming entries (FileType=20). User then opens rekordbox
manually and runs Track → Analyze; rekordbox produces ANLZ files containing
PSSI phrase tags + auto-placed memory/hot cues. Those get ingested via
`dj detect import-rekordbox-analysis` later.

We do NOT push our own cue points / hot cues into rekordbox here — that would
shadow whatever rekordbox computes on its own. Tracks land bare; rekordbox
fills in everything during analysis.

Constraint: rekordbox MUST be quit (locks master.db while open). Pre-flight
check aborts with a clear message.

Idempotent: skip rule is rekordbox_export_at IS NULL — re-running picks up
only new tracks. --force overrides.
"""
from __future__ import annotations

import datetime
from pathlib import Path
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

from detect import db as detect_db
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

    Strip the trailing 'MM:SS' duration our table sometimes appends, then
    pull off the last (...) block as Subtitle.
    """
    t = title.strip()
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

    detect_db.migrate()
    pending = detect_db.get_export_to_rekordbox_pending(table=table, force=force)
    rows = [dict(r) for r in pending]
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print(
            f"Nothing to export — every track in {table} already has rekordbox_export_at set.\n"
            "[dim]Use --force to re-push all tracks.[/dim]"
        )
        return

    console.print(
        f"[bold]export-to-rekordbox[/bold] ← {len(rows)} tracks from [cyan]{table}[/cyan]"
        f"{' [yellow](forced)[/yellow]' if force else ''}"
    )
    console.print(f"  playlist: [yellow]{playlist_name}[/yellow]")
    if dry_run:
        console.print("  [dim]DRY RUN — no writes[/dim]")

    # Lazy import — pyrekordbox pulls heavy deps.
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()
    try:
        device = db.get_device().first()
        if device is None:
            console.print("[red]No rekordbox device — is rekordbox set up?[/red]")
            return

        if not dry_run:
            backup = backup_db(playlist_name)
            if backup is None:
                console.print("[red]Backup failed — aborting.[/red]")
                return
            console.print(f"[dim]Backed up master.db → {backup}[/dim]")

        existing_pl = db.get_playlist(Name=playlist_name).first()
        if existing_pl is not None:
            console.print(f"[dim]Reusing playlist (id={existing_pl.ID})[/dim]")
            playlist = existing_pl
        else:
            playlist = None if dry_run else db.create_playlist(name=playlist_name)
            if playlist is not None:
                console.print(f"[dim]Created playlist (id={playlist.ID})[/dim]")

        existing_track_ids: set[str] = set()
        if playlist is not None:
            existing_songs = db.get_playlist_songs(PlaylistID=playlist.ID).all()
            existing_track_ids = {str(s.ContentID) for s in existing_songs}

        progress = Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TaskProgressColumn(),
            console=console,
        )

        counts = {"new": 0, "existing_track": 0, "added_to_playlist": 0,
                  "already_in_playlist": 0}

        with progress:
            t = progress.add_task("Adding tracks…", total=len(rows))
            for pos, row in enumerate(rows, 1):
                progress.update(
                    t, advance=1,
                    description=f"{row['artist']} — {row['title']}"[:60],
                )
                bid = row["beatport_id"]
                folder_path = f"/v4/catalog/tracks/{bid}/"
                content = db.get_content(FolderPath=folder_path).first()

                if content is None:
                    counts["new"] += 1
                    if dry_run:
                        continue
                    content = _create_beatport_content(db, tables, row, folder_path, device)
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
        console.print()
        console.print(
            "[dim]Next:[/dim] open rekordbox → find the [yellow]" + playlist_name + "[/yellow] playlist → "
            "right-click → [cyan]Analyze Tracks[/cyan]. When done, "
            "run [cyan]dj detect import-rekordbox-analysis --table " + table + "[/cyan] to ingest "
            "the phrase + cue data back into the DB."
        )
    finally:
        db.close()


def _create_beatport_content(db, tables, row, folder_path, device):
    """Bare Beatport streaming entry — NO cueData/hotCuePoints. Rekordbox
    will compute its own during Analyze Tracks."""
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
