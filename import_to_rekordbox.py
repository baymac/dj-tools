#!/usr/bin/env python3
"""
DJ Studio to Rekordbox Importer (via pyrekordbox)

Two-pass import pipeline:
  Pass 1: Create tracks, playlist, and transition effects in rekordbox.
  Pass 2: Write hot cue points snapped to rekordbox's analyzed beatgrid.

Between passes, open rekordbox to analyze all tracks so ANLZ beatgrids
are generated. Pass 2 reads the PQTZ tag and snaps cues to the nearest
beat via binary search.

Missing Beatport tracks are automatically created in the DB as streaming
entries (FileType=20), matching the format rekordbox uses natively.

Usage:
    python3 import_to_rekordbox.py mix.json                 Pass 1
    python3 import_to_rekordbox.py mix.json --cues-only     Pass 2 (snapped)
    python3 import_to_rekordbox.py mix.json --cues-only --no-snap  Pass 2 (unsnapped)
    Add --dry-run to any command to preview without writing.

Requirements:
    - pyrekordbox (pip install pyrekordbox)
    - Rekordbox must be CLOSED before running (for writes)
"""

import bisect
import json
import re
import shutil
import subprocess
import sys
import argparse
import datetime
from pathlib import Path
from uuid import uuid4
from typing import Dict, List, Optional, Tuple


from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables


# Camelot key string -> DjmdKey ScaleName
# Rekordbox stores keys using its own ScaleName values. For Beatport streaming
# tracks, the key is stored as Camelot notation (e.g., "11A", "6B").
CAMELOT_KEYS = [
    "1A", "2A", "3A", "4A", "5A", "6A", "7A", "8A", "9A", "10A", "11A", "12A",
    "1B", "2B", "3B", "4B", "5B", "6B", "7B", "8B", "9B", "10B", "11B", "12B",
]

# Hot cue letter -> Kind value in DjmdCue (Kind > 0 = hot cue)
CUE_KIND = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8}

# Effects that indicate a bass swap is present in a transition
BASS_SWAP_EFFECTS = {"AE_Bass_Swap", "AE_Bass_SwapFade", "AE_Bass_CrossFade"}

RB6 = Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox6"
RB_DB_PATH = RB6 / "master.db"
RB_BACKUP_DIR = RB6 / "claude-backups"


def backup_db(label: str) -> Optional[Path]:
    """Copy master.db to a timestamped backup before a write operation."""
    if not RB_DB_PATH.exists():
        return None
    RB_BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^\w-]", "_", label)[:40].strip("_")
    dest = RB_BACKUP_DIR / f"{ts}_{slug}.db"
    shutil.copy2(RB_DB_PATH, dest)
    return dest


class RekordboxImporter:
    """Import DJ Studio mixes into rekordbox database."""

    MY_TAG_PARENT_ID = "4"  # "Untitled Column" for key tags
    PREP_BARS = 8  # Default bars before transition start; overridden per genre

    # Genre substring (lowercase) → bars before transition start
    GENRE_PREP_BARS: Dict[str, int] = {
        # Techno / industrial: slow-moving, needs a 16-bar runway
        "techno": 16,
        "industrial": 16,
        # Trance: long peak-time builds
        "trance": 16,
        "psytrance": 16,
        # House / afro / melodic: standard 8-bar phrasing
        "house": 8,
        "disco": 8,
        "afro": 8,
        "melodic": 8,
        "electronica": 8,
        # Fast-phrased or bar-dense genres: 4 bars is plenty
        "drum and bass": 4,
        "dnb": 4,
        "jungle": 4,
        "hip hop": 4,
        "hip-hop": 4,
        "trap": 4,
        "r&b": 4,
        "reggaeton": 4,
        "breakbeat": 4,
    }

    def __init__(self, dry_run: bool = False, cues_only: bool = False, snap: bool = True):
        self.dry_run = dry_run
        self.cues_only = cues_only
        self.snap = snap
        # Always open DB (needed for reading/matching even in dry-run)
        self.db = Rekordbox6Database()
        # Cache lookups
        self._device = None
        self._key_cache = {}  # ScaleName -> DjmdKey
        self._mytag_cache = {}  # key name (e.g. "11A") -> DjmdMyTag

    def close(self):
        if self.db is not None:
            self.db.close()

    def _prep_bars_for(self, genre: Optional[str], duration_beats: float) -> int:
        """Bars before transition start, tuned to genre and transition length.

        Caps at half the transition duration in bars so the prep cue never
        lands before the previous transition's end cue.
        """
        bars = self.PREP_BARS
        if genre:
            g = genre.lower()
            for keyword, mapped in self.GENRE_PREP_BARS.items():
                if keyword in g:
                    bars = mapped
                    break
        max_bars = max(2, int(duration_beats / 8))
        return min(bars, max_bars)

    @property
    def device(self):
        if self._device is None:
            self._device = self.db.get_device().first()
        return self._device

    def find_track_by_beatport_id(self, beatport_id: str) -> Optional[tables.DjmdContent]:
        """Find a track in rekordbox DB by its Beatport ID."""
        folder_path = f"/v4/catalog/tracks/{beatport_id}/"
        results = self.db.get_content(FolderPath=folder_path)
        return results.first()

    def get_or_create_artist(self, name: str) -> tables.DjmdArtist:
        """Find existing artist or create a new one."""
        existing = self.db.get_artist(Name=name).first()
        if existing:
            return existing
        return self.db.add_artist(name=name)

    def get_or_create_genre(self, name: str) -> tables.DjmdGenre:
        """Find existing genre or create a new one."""
        existing = self.db.get_genre(Name=name).first()
        if existing:
            return existing
        return self.db.add_genre(name=name)

    def get_key_id(self, camelot_key: str) -> Optional[str]:
        """Look up DjmdKey ID for a Camelot key string like '11A'."""
        if not camelot_key or camelot_key == "N/A":
            return None
        if camelot_key in self._key_cache:
            return self._key_cache[camelot_key].ID

        key_obj = self.db.get_key(ScaleName=camelot_key).first()
        if key_obj:
            self._key_cache[camelot_key] = key_obj
            return key_obj.ID
        return None

    def create_beatport_track(self, beatport_id: str, track_info: Dict) -> tables.DjmdContent:
        """Create a new Beatport streaming track entry in the DB.

        Mimics the structure rekordbox uses for Beatport streaming tracks:
        FileType=20, FolderPath=/v4/catalog/tracks/{ID}/
        """
        folder_path = f"/v4/catalog/tracks/{beatport_id}/"

        # Generate IDs (DB stores IDs as strings)
        content_id = str(self.db.generate_unused_id(tables.DjmdContent))
        file_id = str(self.db.generate_unused_id(tables.DjmdContent, id_field_name="rb_file_id"))
        content_uuid = str(uuid4())

        # Get or create artist
        artist_name = track_info.get("artist", "Unknown")
        artist = self.get_or_create_artist(artist_name)

        # Get or create genre
        genre_name = track_info.get("genre", "")
        genre_id = None
        if genre_name:
            genre = self.get_or_create_genre(genre_name)
            genre_id = genre.ID

        # Key lookup
        camelot_key = track_info.get("key", "")
        key_id = self.get_key_id(camelot_key)

        # BPM: rekordbox stores as int * 100
        bpm_raw = track_info.get("bpm", 0)
        bpm = int(bpm_raw * 100) if bpm_raw else 0

        # Length in seconds (integer)
        duration = track_info.get("duration", 0)
        length = int(duration)

        # Parse title - strip remix/mix info into Subtitle
        title = track_info.get("title", "Unknown")
        subtitle = ""
        # Common patterns: "Title (Extended Mix)", "Title (Original Mix)"
        if "(" in title and title.endswith(")"):
            paren_start = title.rfind("(")
            subtitle = title[paren_start + 1 : -1].strip()
            title = title[:paren_start].strip()

        commnt = ""

        today = str(datetime.date.today())

        content = tables.DjmdContent.create(
            ID=content_id,
            UUID=content_uuid,
            FolderPath=folder_path,
            FileNameL=folder_path,
            FileNameS="",
            Title=title,
            Subtitle=subtitle,
            ArtistID=artist.ID,
            OrgArtistID=artist.ID,
            GenreID=genre_id,
            KeyID=key_id,
            BPM=bpm,
            Length=length,
            FileType=20,
            FileSize=0,
            BitRate=0,
            BitDepth=16,
            SampleRate=44100,
            Rating=0,
            Commnt=commnt,
            ColorID="0",
            StockDate=today,
            DateCreated="",
            Analysed=0,
            DJPlayCount=0,
            TrackNo=0,
            DiscNo=0,
            DeviceID=self.device.ID,
            MasterDBID=self.device.MasterDBID,
            MasterSongID=content_id,
            rb_file_id=file_id,
            HotCueAutoLoad="on",
            DeliveryControl="on",
            DeliveryComment="",
            ContentLink=0,
            ExtInfo='{"StreamingInfo": {"AudioQuality": "0", "AudioQualityWhenAnalyzed": "0"}}',
        )
        self.db.add(content)
        self.db.flush()
        return content

    def create_playlist(self, name: str) -> tables.DjmdPlaylist:
        """Create a new playlist in rekordbox."""
        return self.db.create_playlist(name=name)

    def add_track_to_playlist(
        self,
        playlist: tables.DjmdPlaylist,
        content: tables.DjmdContent,
        position: int,
    ):
        """Add a track to a playlist at a given position."""
        self.db.add_to_playlist(
            playlist=playlist, content=content, track_no=position
        )

    # Effects to include in comments (volume and bass only)
    COMMENT_EFFECTS = {
        "AE_CrossFade", "AE_FadeIn", "AE_FadeOut", "AE_Swap",
        "AE_Bass_CrossFade", "AE_Bass_FadeOut", "AE_Bass_Swap", "AE_Bass_SwapFade",
    }

    def set_track_effects(
        self,
        content: tables.DjmdContent,
        outgoing_effects: Optional[List[str]],
        incoming_effects: Optional[List[str]],
    ):
        """Write transition effect names into the track's Commnt field.

        Only includes volume and bass effects (no stems, filters, etc.).
        Format: Trans out: CrossFade, Bass_CrossFade | Trans in: CrossFade
        """
        parts = []

        if outgoing_effects:
            filtered = [e for e in outgoing_effects if e in self.COMMENT_EFFECTS]
            if filtered:
                clean = [e.replace("AE_", "") for e in filtered]
                parts.append(f"Trans out: {', '.join(clean)}")

        if incoming_effects:
            filtered = [e for e in incoming_effects if e in self.COMMENT_EFFECTS]
            if filtered:
                clean = [e.replace("AE_", "") for e in filtered]
                parts.append(f"Trans in: {', '.join(clean)}")

        content.Commnt = " | ".join(parts)

    # ── Hot cue helpers ──────────────────────────────────────────────────

    @staticmethod
    def beats_to_ms(beat: float, bpm: float) -> int:
        """Convert a beat position to milliseconds, snapped to nearest whole beat.

        DJ Studio's beat grid doesn't align with rekordbox's, so beat positions
        may be fractional (e.g. 128.3 instead of 128). Rounding to the nearest
        integer beat ensures cues land on exact beat boundaries in rekordbox,
        even if the track hasn't been analysed yet.
        """
        if bpm <= 0:
            return 0
        return int(round(beat) * 60000.0 / bpm)

    @staticmethod
    def snap_to_beatgrid(ms: int, beat_times_ms: List[float]) -> int:
        """Snap a millisecond position to the nearest beat in Rekordbox's grid."""
        if not beat_times_ms:
            return ms
        idx = bisect.bisect_left(beat_times_ms, ms)
        # Edge cases: before first or after last beat
        if idx == 0:
            return int(beat_times_ms[0])
        if idx >= len(beat_times_ms):
            return int(beat_times_ms[-1])
        # Pick the closer of the two surrounding beats
        before = beat_times_ms[idx - 1]
        after = beat_times_ms[idx]
        if (ms - before) <= (after - ms):
            return int(before)
        return int(after)

    def snapped_beats_to_ms(self, beat: float, bpm: float, beat_times_ms: Optional[List[float]]) -> int:
        """Convert beat to ms, optionally snapping to Rekordbox's analyzed beatgrid."""
        raw_ms = self.beats_to_ms(beat, bpm)
        if beat_times_ms is not None and self.snap:
            return self.snap_to_beatgrid(raw_ms, beat_times_ms)
        return raw_ms

    def get_beatgrid(self, content: "tables.DjmdContent") -> Optional[List[float]]:
        """Read ANLZ beatgrid for a track, returning downbeat (beat 1) times in ms.

        Only includes beat 1 of each bar so that cue snapping lands on bar
        boundaries. Returns None if no ANLZ files exist, no PQTZ tag found,
        or data is corrupt.
        """
        try:
            anlz_files = self.db.read_anlz_files(content)
            for anlz_file in anlz_files.values():
                if "PQTZ" in anlz_file:
                    beats, bpms, times = anlz_file.get("PQTZ")
                    # Filter to beat 1 (downbeats) only; times are in seconds
                    return sorted(
                        float(t) * 1000.0
                        for b, t in zip(beats, times)
                        if int(b) == 1
                    )
        except Exception:
            pass
        return None

    @staticmethod
    def has_bass_swap(effects: List[str]) -> bool:
        """Check if any bass swap effect is present in the transition."""
        return bool(BASS_SWAP_EFFECTS.intersection(effects))

    def clear_hot_cues(self, content: tables.DjmdContent):
        """Remove all existing hot cues from a track."""
        existing = self.db.get_cue(ContentID=content.ID).all()
        for cue in existing:
            if cue.is_hot_cue:
                self.db.delete(cue)
        self.db.flush()

    def add_hot_cue(
        self,
        content: tables.DjmdContent,
        cue_letter: str,
        position_ms: int,
        comment: str = "",
    ):
        """Add a single hot cue to a track in the rekordbox database."""
        cue_id = str(uuid4())
        now = datetime.datetime.now()

        cue = tables.DjmdCue.create(
            ID=cue_id,
            UUID=str(uuid4()),
            ContentID=content.ID,
            ContentUUID=getattr(content, "UUID", ""),
            InMsec=max(0, position_ms),
            InFrame=0,
            InMpegFrame=0,
            InMpegAbs=0,
            OutMsec=-1,
            OutFrame=-1,
            OutMpegFrame=-1,
            OutMpegAbs=-1,
            Kind=CUE_KIND[cue_letter],
            Color=-1,
            ColorTableIndex=0,
            ActiveLoop=0,
            Comment=comment,
            BeatLoopSize=0,
            CueMicrosec=0,
            InPointSeekInfo="",
            OutPointSeekInfo="",
            created_at=now,
            updated_at=now,
        )
        self.db.add(cue)

    def _add_transition_cues(
        self,
        content: tables.DjmdContent,
        bpm: float,
        anchor_beat: float,
        transition: Dict,
        cue_prep: str,
        cue_start: str,
        cue_bass: str,
        cue_end: str,
        beat_times_ms: Optional[List[float]] = None,
        genre: Optional[str] = None,
    ):
        """Add cue points for a transition.

        anchor_beat is the beat where the transition starts (end_beat for
        outgoing, start_beat for incoming). The transition extends forward
        from anchor_beat by duration_beats.

        Cue layout (e.g. A-D or E-H):
          prep  = genre-tuned bars before transition start
          start = transition start
          bass  = bass swap position (only if bass swap effect present)
          end   = transition end
        """
        duration_beats = transition["duration_beats"]
        effects = transition.get("effects", [])
        effect_offset = transition.get("effect_offset", 0)
        prep_beats = self._prep_bars_for(genre, duration_beats) * 4

        self.add_hot_cue(
            content, cue_prep,
            self.snapped_beats_to_ms(anchor_beat - prep_beats, bpm, beat_times_ms), "Prep",
        )
        self.add_hot_cue(
            content, cue_start,
            self.snapped_beats_to_ms(anchor_beat, bpm, beat_times_ms), "Trans Start",
        )
        if self.has_bass_swap(effects):
            bass_offset = effect_offset if effect_offset > 0 else duration_beats / 2
            self.add_hot_cue(
                content, cue_bass,
                self.snapped_beats_to_ms(anchor_beat + bass_offset, bpm, beat_times_ms), "Bass Swap",
            )
        self.add_hot_cue(
            content, cue_end,
            self.snapped_beats_to_ms(anchor_beat + duration_beats, bpm, beat_times_ms), "Trans End",
        )

    def set_track_cues(
        self,
        content: tables.DjmdContent,
        track: Dict,
        incoming_trans: Optional[Dict],
        outgoing_trans: Optional[Dict],
        is_first: bool,
        beat_times_ms: Optional[List[float]] = None,
    ):
        """Set hot cue points on a track.

        Cue layout:
          A-D = incoming transition (A=prep, B=start, C=bass swap, D=end)
          E-H = outgoing transition (E=prep, F=start, G=bass swap, H=end)

        Outgoing transition starts at end_beat and extends forward.
        If a transition or bass swap doesn't exist, those letters are left empty.
        """
        bpm = track.get("bpm", 0)
        if not bpm or bpm <= 0:
            return

        genre = track.get("genre")
        start_beat = track.get("start_beat", 0)
        end_beat = track.get("end_beat", 0)

        self.clear_hot_cues(content)

        if incoming_trans:
            self._add_transition_cues(
                content, bpm, start_beat, incoming_trans,
                cue_prep="A", cue_start="B", cue_bass="C", cue_end="D",
                beat_times_ms=beat_times_ms, genre=genre,
            )
        if outgoing_trans:
            self._add_transition_cues(
                content, bpm, end_beat, outgoing_trans,
                cue_prep="E", cue_start="F", cue_bass="G", cue_end="H",
                beat_times_ms=beat_times_ms, genre=genre,
            )

        self.db.flush()

    def preview_track_cues(
        self,
        track: Dict,
        incoming_trans: Optional[Dict],
        outgoing_trans: Optional[Dict],
        is_first: bool,
        beat_times_ms: Optional[List[float]] = None,
    ) -> List[Dict]:
        """Compute cue points for a track without writing to DB (for dry run)."""
        bpm = track.get("bpm", 0)
        if not bpm or bpm <= 0:
            return []

        genre = track.get("genre")
        start_beat = track.get("start_beat", 0)
        end_beat = track.get("end_beat", 0)
        snapped = beat_times_ms is not None and self.snap
        cues = []

        def add(letter, ms, label):
            cues.append({"letter": letter, "ms": max(0, ms), "label": label, "snapped": snapped})

        def add_transition(anchor, trans, prep_l, start_l, bass_l, end_l):
            d = trans["duration_beats"]
            effects = trans.get("effects", [])
            eo = trans.get("effect_offset", 0)
            prep_beats = self._prep_bars_for(genre, d) * 4
            add(prep_l, self.snapped_beats_to_ms(anchor - prep_beats, bpm, beat_times_ms), "Prep")
            add(start_l, self.snapped_beats_to_ms(anchor, bpm, beat_times_ms), "Trans Start")
            if self.has_bass_swap(effects):
                bo = eo if eo > 0 else d / 2
                add(bass_l, self.snapped_beats_to_ms(anchor + bo, bpm, beat_times_ms), "Bass Swap")
            add(end_l, self.snapped_beats_to_ms(anchor + d, bpm, beat_times_ms), "Trans End")

        if incoming_trans:
            add_transition(start_beat, incoming_trans, "A", "B", "C", "D")
        if outgoing_trans:
            add_transition(end_beat, outgoing_trans, "E", "F", "G", "H")

        return cues

    def import_mix(self, json_data: Dict) -> Dict:
        """Orchestrate the full import process.

        Returns a report dict with matched/unmatched/created tracks and actions taken.
        """
        metadata = json_data["metadata"]
        tracks = json_data["tracks"]
        transitions = json_data.get("transitions", [])
        mix_name = metadata["name"]

        if not self.dry_run:
            bp = backup_db(mix_name)
            if bp:
                print(f"Backup: {bp.name}")

        # Build a lookup: transition number -> transition data
        trans_by_num = {t["number"]: t for t in transitions}

        report = {
            "mix_name": mix_name,
            "total_tracks": len(tracks),
            "matched": [],
            "created": [],
            "unmatched": [],
            "playlist_created": False,
            "effects_written": 0,
        }

        # Step 1: Match tracks, create missing Beatport tracks
        matched_contents: List[Tuple[Dict, Optional[tables.DjmdContent]]] = []

        for track in sorted(tracks, key=lambda t: t["position"]):
            library_key = track.get("library_key", "")
            beatport_id = None

            if "beatport-sdk_" in library_key:
                beatport_id = library_key.split("_", 1)[1]

            content = None
            if beatport_id:
                content = self.find_track_by_beatport_id(beatport_id)

            if content is not None:
                report["matched"].append({
                    "position": track["position"],
                    "title": track.get("title", "?"),
                    "artist": track.get("artist", "?"),
                    "beatport_id": beatport_id,
                    "rb_id": content.ID,
                })
            elif beatport_id and not self.dry_run:
                # Create the missing track
                content = self.create_beatport_track(beatport_id, track)
                report["created"].append({
                    "position": track["position"],
                    "title": track.get("title", "?"),
                    "artist": track.get("artist", "?"),
                    "beatport_id": beatport_id,
                    "rb_id": content.ID,
                })
            else:
                report["unmatched"].append({
                    "position": track["position"],
                    "title": track.get("title", "?"),
                    "artist": track.get("artist", "?"),
                    "beatport_id": beatport_id,
                    "library_key": library_key,
                })

            matched_contents.append((track, content))

        # Step 2: Create playlist
        playlist = None
        if not self.dry_run and any(c is not None for _, c in matched_contents):
            playlist = self.create_playlist(mix_name)
            report["playlist_created"] = True

        # Step 3: Add tracks to playlist and write effects
        for track, content in matched_contents:
            if content is None:
                continue

            pos = track["position"]

            if playlist is not None:
                self.add_track_to_playlist(playlist, content, pos)

            # Transition N is between track N and track N+1
            outgoing = trans_by_num.get(pos)
            incoming = trans_by_num.get(pos - 1)

            outgoing_effects = outgoing["effects"] if outgoing else None
            incoming_effects = incoming["effects"] if incoming else None

            if outgoing_effects or incoming_effects:
                if self.dry_run:
                    clean_out = (
                        [e.replace("AE_", "") for e in outgoing_effects
                         if e in self.COMMENT_EFFECTS]
                        if outgoing_effects
                        else []
                    )
                    clean_in = (
                        [e.replace("AE_", "") for e in incoming_effects
                         if e in self.COMMENT_EFFECTS]
                        if incoming_effects
                        else []
                    )
                    for m in report["matched"]:
                        if m["position"] == pos:
                            m["effects_out"] = clean_out
                            m["effects_in"] = clean_in
                else:
                    self.set_track_effects(content, outgoing_effects, incoming_effects)
                report["effects_written"] += 1

        # Step 4: Commit
        if not self.dry_run:
            self.db.commit()

        return report

    def import_cues_only(self, json_data: Dict) -> Dict:
        """Pass 2: find existing tracks, read ANLZ beatgrids, snap & write cues.

        Assumes tracks and playlist already exist from Pass 1.
        """
        tracks = json_data["tracks"]
        transitions = json_data.get("transitions", [])
        mix_name = json_data["metadata"]["name"]

        if not self.dry_run:
            bp = backup_db(mix_name + "_cues")
            if bp:
                print(f"Backup: {bp.name}")

        trans_by_num = {t["number"]: t for t in transitions}

        report = {
            "mix_name": mix_name,
            "total_tracks": len(tracks),
            "found": [],
            "not_found": [],
            "no_beatgrid": [],
            "cues_written": 0,
        }

        for track in sorted(tracks, key=lambda t: t["position"]):
            library_key = track.get("library_key", "")
            beatport_id = None
            if "beatport-sdk_" in library_key:
                beatport_id = library_key.split("_", 1)[1]

            content = None
            if beatport_id:
                content = self.find_track_by_beatport_id(beatport_id)

            if content is None:
                report["not_found"].append({
                    "position": track["position"],
                    "title": track.get("title", "?"),
                    "artist": track.get("artist", "?"),
                    "beatport_id": beatport_id,
                })
                continue

            pos = track["position"]
            is_first = pos == 1
            outgoing = trans_by_num.get(pos)
            incoming = trans_by_num.get(pos - 1)

            # Read Rekordbox's analyzed beatgrid
            beat_times_ms = self.get_beatgrid(content)
            snapped = beat_times_ms is not None and self.snap

            if beat_times_ms is None:
                report["no_beatgrid"].append(track.get("title", "?"))

            cue_preview = self.preview_track_cues(
                track, incoming, outgoing, is_first, beat_times_ms=beat_times_ms,
            )

            if not self.dry_run:
                self.set_track_cues(
                    content, track, incoming, outgoing, is_first,
                    beat_times_ms=beat_times_ms,
                )

            report["cues_written"] += 1
            report["found"].append({
                "position": pos,
                "title": track.get("title", "?"),
                "artist": track.get("artist", "?"),
                "beatport_id": beatport_id,
                "rb_id": content.ID,
                "snapped": snapped,
                "cues": cue_preview,
            })

        if not self.dry_run:
            self.db.commit()

        return report


def _fmt_ms(ms: int) -> str:
    """Format milliseconds as M:SS.s"""
    s = ms / 1000.0
    m = int(s // 60)
    s -= m * 60
    return f"{m}:{s:04.1f}"


def _print_track_entry(entry: Dict, indent: str = "  "):
    """Print a single track line with optional effects and cues."""
    effects = ""
    if entry.get("effects_out") or entry.get("effects_in"):
        parts = []
        if entry.get("effects_out"):
            parts.append(f"out: {', '.join(entry['effects_out'])}")
        if entry.get("effects_in"):
            parts.append(f"in: {', '.join(entry['effects_in'])}")
        effects = f"  [{'; '.join(parts)}]"
    suffix = f" (BP:{entry['beatport_id']})" if "beatport_id" in entry else ""
    print(f"{indent}{entry['position']:2}. {entry['artist']} - {entry['title']}{suffix}{effects}")

    cues = entry.get("cues", [])
    if cues:
        cue_strs = [f"{c['letter']}={_fmt_ms(c['ms'])}({c['label']})" for c in cues]
        print(f"{indent}    Cues: {', '.join(cue_strs)}")


def print_report(report: Dict, dry_run: bool):
    """Pretty-print the import report."""
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"\n{'=' * 70}")
    print(f"{prefix}Import Report: {report['mix_name']}")
    print(f"{'=' * 70}")

    matched = len(report["matched"])
    created = len(report["created"])
    unmatched = len(report["unmatched"])

    print(f"\nTracks: {report['total_tracks']} total, "
          f"{matched} found in DB, "
          f"{created} created, "
          f"{unmatched} skipped")

    if report["matched"]:
        print(f"\nAlready in rekordbox:")
        for m in report["matched"]:
            _print_track_entry(m)

    if report["created"]:
        print(f"\nCreated in rekordbox:")
        for c in report["created"]:
            _print_track_entry(c)

    if report["unmatched"]:
        print(f"\nSkipped (no Beatport ID):")
        for u in report["unmatched"]:
            bp = f" (Beatport: {u['beatport_id']})" if u.get("beatport_id") else ""
            print(f"  {u['position']:2}. {u['artist']} - {u['title']}{bp}")

    if not dry_run:
        if report["playlist_created"]:
            print(f"\nPlaylist '{report['mix_name']}' created.")
        if report["created"]:
            print(f"{created} new tracks added to rekordbox collection.")
        print(f"Effects written to {report['effects_written']} tracks.")
        print(f"\nNext steps:")
        print(f"  1. Open rekordbox and let it analyze all tracks in the playlist")
        print(f"  2. Once analysis is complete, close rekordbox and run:")
        print(f"     python3 import_to_rekordbox.py {report['mix_name']}.json --cues-only")
    else:
        if unmatched > 0 and any(u.get("beatport_id") for u in report["unmatched"]):
            print(f"\n{unmatched} tracks will be created in rekordbox on actual run.")
        print(f"\nNo changes made (dry run).")

    print(f"{'=' * 70}\n")


def print_cues_report(report: Dict, dry_run: bool):
    """Pretty-print the cues-only (Pass 2) report."""
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"\n{'=' * 70}")
    print(f"{prefix}Cues Report: {report['mix_name']}")
    print(f"{'=' * 70}")

    found = len(report["found"])
    not_found = len(report["not_found"])
    snapped_count = sum(1 for t in report["found"] if t["snapped"])
    unsnapped_count = found - snapped_count

    print(f"\nTracks: {report['total_tracks']} total, "
          f"{found} found in DB, "
          f"{not_found} not found")

    if report["found"]:
        print(f"\nCue points:")
        for entry in report["found"]:
            snap_tag = "[SNAPPED]" if entry["snapped"] else "[unsnapped]"
            suffix = f" (BP:{entry['beatport_id']})" if entry.get("beatport_id") else ""
            print(f"  {entry['position']:2}. {entry['artist']} - {entry['title']}{suffix}  {snap_tag}")
            cues = entry.get("cues", [])
            if cues:
                cue_strs = [f"{c['letter']}={_fmt_ms(c['ms'])}({c['label']})" for c in cues]
                print(f"      Cues: {', '.join(cue_strs)}")

    if report["not_found"]:
        print(f"\nNot found in DB (run Pass 1 first):")
        for entry in report["not_found"]:
            bp = f" (BP:{entry['beatport_id']})" if entry.get("beatport_id") else ""
            print(f"  {entry['position']:2}. {entry['artist']} - {entry['title']}{bp}")

    if report["no_beatgrid"]:
        print(f"\nNo beatgrid (analyze in rekordbox first):")
        for title in report["no_beatgrid"]:
            print(f"  - {title}")

    print(f"\nSummary: {snapped_count} snapped, {unsnapped_count} unsnapped")

    if not dry_run:
        print(f"Hot cues written to {report['cues_written']} tracks.")
    else:
        print(f"\nNo changes made (dry run).")

    print(f"{'=' * 70}\n")


def cmd_undo_list():
    if not RB_BACKUP_DIR.exists() or not list(RB_BACKUP_DIR.glob("*.db")):
        print(f"No backups found in {RB_BACKUP_DIR}")
        return
    backups = sorted(RB_BACKUP_DIR.glob("*.db"))
    print(f"Available backups ({RB_BACKUP_DIR}):\n")
    for b in backups:
        size_mb = b.stat().st_size / (1024 * 1024)
        print(f"  {b.name}  ({size_mb:.1f} MB)")
    print(f"\nRestore: uv run import_to_rekordbox.py undo restore BACKUP_NAME")


def cmd_undo_restore(backup_name: str):
    if subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True).returncode == 0:
        print("ERROR: rekordbox is running. Close it before restoring.", file=sys.stderr)
        sys.exit(1)

    backup_path = RB_BACKUP_DIR / backup_name
    if not backup_path.exists():
        matches = list(RB_BACKUP_DIR.glob(f"*{backup_name}*"))
        if len(matches) == 1:
            backup_path = matches[0]
        elif len(matches) > 1:
            print(f"Ambiguous: multiple backups match '{backup_name}':", file=sys.stderr)
            for m in matches:
                print(f"  {m.name}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Backup not found: {backup_name}", file=sys.stderr)
            sys.exit(1)

    if not RB_DB_PATH.exists():
        print(f"rekordbox DB not found at {RB_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    pre = backup_db("pre-restore")
    if pre:
        print(f"Saved current DB as: {pre.name}")
    shutil.copy2(backup_path, RB_DB_PATH)
    print(f"Restored {backup_path.name}  →  {RB_DB_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Import DJ Studio mix JSON into rekordbox database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Two-pass workflow:
  Pass 1: %(prog)s mix.json              Create tracks, playlist, effects
  Pass 2: %(prog)s mix.json --cues-only  Write cues snapped to rekordbox beatgrid

Between passes, open rekordbox and analyze all tracks in the playlist.

Examples:
  %(prog)s mix.json --dry-run              Preview Pass 1
  %(prog)s mix.json                        Run Pass 1
  %(prog)s mix.json --cues-only --dry-run  Preview Pass 2 (with snap status)
  %(prog)s mix.json --cues-only            Run Pass 2 (snapped cues)
  %(prog)s mix.json --cues-only --no-snap  Run Pass 2 (unsnapped fallback)

Input JSON is generated by:
  python3 get_mix_info.py "Mix Name" -o mix.json

IMPORTANT: Close rekordbox before running this script!
        """,
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    undo_p = subparsers.add_parser("undo", help="List or restore from DB backups")
    undo_sub = undo_p.add_subparsers(dest="undo_command")
    undo_sub.add_parser("list", help="List available backups")
    undo_restore_p = undo_sub.add_parser("restore", help="Restore from a backup")
    undo_restore_p.add_argument("backup", help="Backup filename (from 'undo list')")

    parser.add_argument("json_file", nargs="?", help="Path to mix JSON file (from get_mix_info.py)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing to DB",
    )
    parser.add_argument(
        "--cues-only",
        action="store_true",
        help="Pass 2: write cues only (snapped to rekordbox beatgrid)",
    )
    parser.add_argument(
        "--no-snap",
        action="store_true",
        help="With --cues-only: skip beatgrid snapping (use raw beat positions)",
    )

    args = parser.parse_args()

    if args.subcommand == "undo":
        if not getattr(args, "undo_command", None):
            undo_p.print_help()
        elif args.undo_command == "list":
            cmd_undo_list()
        elif args.undo_command == "restore":
            cmd_undo_restore(args.backup)
        return

    if not args.json_file:
        parser.print_help()
        return

    # Read JSON
    json_path = Path(args.json_file)
    if not json_path.exists():
        print(f"Error: File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, "r") as f:
        json_data = json.load(f)

    if "metadata" not in json_data or "tracks" not in json_data:
        print("Error: Invalid JSON format. Expected 'metadata' and 'tracks' keys.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded mix: {json_data['metadata']['name']}")
    print(f"Tracks: {len(json_data['tracks'])}")
    print(f"Transitions: {len(json_data.get('transitions', []))}")

    if args.dry_run:
        print("\n--- DRY RUN MODE ---")

    snap = not args.no_snap
    importer = RekordboxImporter(
        dry_run=args.dry_run, cues_only=args.cues_only, snap=snap,
    )
    try:
        if args.cues_only:
            report = importer.import_cues_only(json_data)
            print_cues_report(report, args.dry_run)
        else:
            report = importer.import_mix(json_data)
            print_report(report, args.dry_run)
    finally:
        importer.close()


if __name__ == "__main__":
    main()
