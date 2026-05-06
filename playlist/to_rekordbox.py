"""Push tracks to a rekordbox playlist as bare Beatport streaming entries.

Rekordbox computes its own beatgrid + cues during Analyze Tracks, so we don't
push any cuepoints / hot cues here. FileType=20 marks the entry as Beatport
streaming — same kind rekordbox creates when you drag a Beatport track from
its in-app browser.

Constraint: rekordbox MUST be quit (locks master.db while open). Pre-flight
check aborts with a clear message.
"""
from __future__ import annotations

import datetime
from typing import Callable, Optional, Sequence
from uuid import uuid4

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
from rekordbox.utils import is_rekordbox_running

_DEFAULT_CONSOLE = Console()


def _split_title(title: str) -> tuple[str, str]:
    """'Like You Do (Original Mix) 05:27' → ('Like You Do', 'Original Mix')."""
    t = title.strip()
    if len(t) > 6 and t[-3] == ":" and t[-6] == " " and t[-2:].isdigit() and t[-5:-3].isdigit():
        t = t[:-6].rstrip()
    subtitle = ""
    if "(" in t and t.endswith(")"):
        idx = t.rfind("(")
        subtitle = t[idx + 1 : -1].strip()
        t = t[:idx].strip()
    return t, subtitle


def push_to_rekordbox(
    rows: Sequence[dict],
    playlist_name: str,
    *,
    dry_run: bool = False,
    on_added: Optional[Callable[[int], None]] = None,
    console: Optional[Console] = None,
) -> dict:
    """Push given rows to a rekordbox playlist.

    Each row dict needs: beatport_id, artist, title, genre, key, bpm, duration_sec.
    on_added (if given) is called with each track's beatport_id after a successful
    add (used by detect's idempotent stamp). Returns a counts dict.
    """
    console = console or _DEFAULT_CONSOLE
    counts = {"new": 0, "existing_track": 0, "added_to_playlist": 0, "already_in_playlist": 0}

    if is_rekordbox_running():
        console.print(
            "[red]rekordbox is currently running.[/red]\n"
            "Quit rekordbox before running this command — it locks master.db while open."
        )
        return counts
    if not rows:
        console.print("[yellow]No tracks to push.[/yellow]")
        return counts

    console.print(
        f"[bold]playlist → rekordbox[/bold] ← {len(rows)} tracks  →  [yellow]{playlist_name}[/yellow]"
    )
    if dry_run:
        console.print("  [dim]DRY RUN — no writes[/dim]")

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()
    try:
        device = db.get_device().first()
        if device is None:
            console.print("[red]No rekordbox device — is rekordbox set up?[/red]")
            return counts

        if not dry_run:
            backup = backup_db(playlist_name)
            if backup is None:
                console.print("[red]Backup failed — aborting.[/red]")
                return counts
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

        with progress:
            t = progress.add_task("Adding tracks…", total=len(rows))
            for pos, row in enumerate(rows, 1):
                progress.update(
                    t, advance=1,
                    description=f"{row.get('artist')} — {row.get('title')}"[:60],
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
                    if not dry_run and on_added:
                        on_added(int(bid))
                    continue

                if not dry_run:
                    db.add_to_playlist(playlist=playlist, content=content, track_no=pos)
                    if on_added:
                        on_added(int(bid))
                counts["added_to_playlist"] += 1

        if not dry_run:
            db.commit()

        console.print()
        console.print("[bold]Done.[/bold]")
        console.print(f"  new tracks created:        {counts['new']}")
        console.print(f"  existing tracks reused:    {counts['existing_track']}")
        console.print(f"  added to playlist:         {counts['added_to_playlist']}")
        console.print(f"  already in playlist:       {counts['already_in_playlist']}")
    finally:
        db.close()
    return counts


def _create_beatport_content(db, tables, row, folder_path, device):
    """Bare Beatport streaming entry — NO cueData/hotCuePoints. Rekordbox
    will compute its own during Analyze Tracks.

    Field defaults reverse-engineered from rekordbox-native Beatport imports
    (744 sampled rows). Critical ones for downstream consumers (e.g. DJ
    Studio's "Add tracks → rekordbox" importer):

    - ExtInfo.AudioQuality / AudioQualityWhenAnalyzed = "1"
        Marks the track as a high-quality streamable Beatport track. DJ Studio
        treats "0" as a preview and won't load it.
    - Analysed = 105
        rekordbox's "Beatport metadata loaded" sentinel. Our previous default
        (0) made tracks look freshly added but not yet metadata-synced, which
        DJ Studio's importer skipped.
    - ServiceID = 0
        Required, not nullable in practice — every native Beatport row has it.
    - Lyricist/ISRC/OrgFolderPath/Reserved1/ModifiedByRBM = ""
      SamplerTrackInfo/SamplerPlayOffset/LyricStatus = 0
      SamplerGain = 0.0
      VideoAssociate = "0"
        Native imports always have these fields populated with these defaults
        (never NULL). Some downstream parsers fail on NULL.
    """
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
    isrc = row.get("isrc") or ""
    release_date = row.get("release_date") or ""
    release_year = None
    if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
        release_year = int(release_date[:4])

    content = tables.DjmdContent.create(
        ID=content_id, UUID=content_uuid,
        FolderPath=folder_path, FileNameL=folder_path, FileNameS="",
        Title=title, Subtitle=subtitle,
        ArtistID=artist.ID, OrgArtistID=artist.ID,
        GenreID=genre_id, KeyID=key_id,
        BPM=bpm, Length=length,
        FileType=20,
        FileSize=0, BitRate=0, BitDepth=16, SampleRate=44100,
        Rating=0, Commnt="", ColorID="0",
        StockDate=today, DateCreated="",
        Analysed=105,
        # AnalysisUpdated/TrackInfoUpdated track rekordbox's metadata-sync version
        # number per row. DJ Studio's "Add tracks → rekordbox" importer skips
        # tracks where TrackInfoUpdated < 2 — interprets the row as "not yet
        # ready for display". Native imports + already-fixed tool entries sit
        # at "2" minimum. Set both to "2" so the row displays in DJ Studio.
        AnalysisUpdated="2", TrackInfoUpdated="2",
        DJPlayCount=0, TrackNo=0, DiscNo=0,
        DeviceID=device.ID, MasterDBID=device.MasterDBID, MasterSongID=content_id,
        AnalysisDataPath=f"/PIONEER/USBANLZ/{content_uuid[:1].lower()}/{content_uuid[1:3].lower()}/{content_uuid}",
        rb_file_id="0",
        HotCueAutoLoad="on", DeliveryControl="on", DeliveryComment="", ContentLink=0,
        ExtInfo='{"StreamingInfo": {"AudioQuality": "1", "AudioQualityWhenAnalyzed": "1"}}',
        ServiceID=0,
        ISRC=isrc, Lyricist="", OrgFolderPath="", Reserved1="", ModifiedByRBM="",
        SamplerTrackInfo=0, SamplerPlayOffset=0, SamplerGain=0.0,
        VideoAssociate="0", LyricStatus=0,
        ReleaseYear=release_year, ReleaseDate=release_date or None,
    )
    db.add(content)
    db.flush()
    return content
