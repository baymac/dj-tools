#!/usr/bin/env python3
"""
DJ Studio to Rekordbox Importer (via pyrekordbox)

Reads DJ Studio mix JSON (from get_mix_info.py) and writes directly into
the rekordbox encrypted database — creating a playlist, adding tracks in
order, and storing transition effect names on the tracks.

Missing Beatport tracks are automatically created in the DB as streaming
entries (FileType=20), matching the format rekordbox uses natively.

Usage:
    python3 import_to_rekordbox.py mix.json
    python3 import_to_rekordbox.py mix.json --dry-run

Requirements:
    - pyrekordbox (pip install pyrekordbox)
    - Rekordbox must be CLOSED before running (for writes)
"""

import json
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


class RekordboxImporter:
    """Import DJ Studio mixes into rekordbox database."""

    MY_TAG_PARENT_ID = "4"  # "Untitled Column" for key tags

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        # Always open DB (needed for reading/matching even in dry-run)
        self.db = Rekordbox6Database()
        # Cache lookups
        self._device = None
        self._key_cache = {}  # ScaleName -> DjmdKey
        self._mytag_cache = {}  # key name (e.g. "11A") -> DjmdMyTag

    def close(self):
        if self.db is not None:
            self.db.close()

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
        """Convert a beat position to milliseconds."""
        if bpm <= 0:
            return 0
        return int(beat * 60000.0 / bpm)

    @staticmethod
    def get_prep_beats(duration_beats: int) -> int:
        """Determine prep-cue distance (8, 16, or 32) based on transition length."""
        if duration_beats >= 32:
            return 32
        if duration_beats >= 16:
            return 16
        return 8

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

    def _add_outgoing_cues(
        self,
        content: tables.DjmdContent,
        bpm: float,
        end_beat: float,
        transition: Dict,
        cue_prep: str,
        cue_start: str,
        cue_bass: str,
        cue_end: str,
    ):
        """Add cue points for an outgoing transition (end of a track)."""
        duration_beats = transition["duration_beats"]
        effects = transition.get("effects", [])
        effect_offset = transition.get("effect_offset", 0)

        trans_start_beat = end_beat - duration_beats
        trans_end_beat = end_beat
        prep_beats = self.get_prep_beats(duration_beats)

        self.add_hot_cue(
            content, cue_prep,
            self.beats_to_ms(trans_start_beat - prep_beats, bpm), "Prep",
        )
        self.add_hot_cue(
            content, cue_start,
            self.beats_to_ms(trans_start_beat, bpm), "Trans Start",
        )
        if self.has_bass_swap(effects):
            bass_offset = effect_offset if effect_offset > 0 else duration_beats / 2
            self.add_hot_cue(
                content, cue_bass,
                self.beats_to_ms(trans_start_beat + bass_offset, bpm), "Bass Swap",
            )
        self.add_hot_cue(
            content, cue_end,
            self.beats_to_ms(trans_end_beat, bpm), "Trans End",
        )

    def _add_incoming_cues(
        self,
        content: tables.DjmdContent,
        bpm: float,
        start_beat: float,
        transition: Dict,
        cue_prep: str,
        cue_start: str,
        cue_bass: str,
        cue_end: str,
    ):
        """Add cue points for an incoming transition (start of a track)."""
        duration_beats = transition["duration_beats"]
        effects = transition.get("effects", [])
        effect_offset = transition.get("effect_offset", 0)

        trans_start_beat = start_beat
        trans_end_beat = start_beat + duration_beats
        prep_beats = self.get_prep_beats(duration_beats)

        self.add_hot_cue(
            content, cue_prep,
            self.beats_to_ms(trans_start_beat - prep_beats, bpm), "Prep",
        )
        self.add_hot_cue(
            content, cue_start,
            self.beats_to_ms(trans_start_beat, bpm), "Trans Start",
        )
        if self.has_bass_swap(effects):
            bass_offset = effect_offset if effect_offset > 0 else duration_beats / 2
            self.add_hot_cue(
                content, cue_bass,
                self.beats_to_ms(trans_start_beat + bass_offset, bpm), "Bass Swap",
            )
        self.add_hot_cue(
            content, cue_end,
            self.beats_to_ms(trans_end_beat, bpm), "Trans End",
        )

    def set_track_cues(
        self,
        content: tables.DjmdContent,
        track: Dict,
        incoming_trans: Optional[Dict],
        outgoing_trans: Optional[Dict],
        is_first: bool,
    ):
        """Set hot cue points on a track following the DJ Studio convention.

        First track (5 cues):  A=play start, B-E=outgoing transition
        Middle tracks (8 cues): B-E=incoming, A/F-H=outgoing
        Last track (4 cues):   B-E=incoming only
        """
        bpm = track.get("bpm", 0)
        if not bpm or bpm <= 0:
            return

        start_beat = track.get("start_beat", 0)
        end_beat = track.get("end_beat", 0)

        self.clear_hot_cues(content)

        if is_first:
            # A = play start position
            self.add_hot_cue(
                content, "A", self.beats_to_ms(start_beat, bpm), "Play Start",
            )
            if outgoing_trans:
                self._add_outgoing_cues(
                    content, bpm, end_beat, outgoing_trans,
                    cue_prep="B", cue_start="C", cue_bass="D", cue_end="E",
                )
        else:
            # Incoming transition → pads B, C, D, E
            if incoming_trans:
                self._add_incoming_cues(
                    content, bpm, start_beat, incoming_trans,
                    cue_prep="B", cue_start="C", cue_bass="D", cue_end="E",
                )
            # Outgoing transition → pads A, F, G, H
            if outgoing_trans:
                self._add_outgoing_cues(
                    content, bpm, end_beat, outgoing_trans,
                    cue_prep="A", cue_start="F", cue_bass="G", cue_end="H",
                )

        self.db.flush()

    def preview_track_cues(
        self,
        track: Dict,
        incoming_trans: Optional[Dict],
        outgoing_trans: Optional[Dict],
        is_first: bool,
    ) -> List[Dict]:
        """Compute cue points for a track without writing to DB (for dry run)."""
        bpm = track.get("bpm", 0)
        if not bpm or bpm <= 0:
            return []

        start_beat = track.get("start_beat", 0)
        end_beat = track.get("end_beat", 0)
        cues = []

        def add(letter, ms, label):
            cues.append({"letter": letter, "ms": max(0, ms), "label": label})

        def add_outgoing(trans, prep_l, start_l, bass_l, end_l):
            d = trans["duration_beats"]
            effects = trans.get("effects", [])
            eo = trans.get("effect_offset", 0)
            ts = end_beat - d
            prep = self.get_prep_beats(d)
            add(prep_l, self.beats_to_ms(ts - prep, bpm), "Prep")
            add(start_l, self.beats_to_ms(ts, bpm), "Trans Start")
            if self.has_bass_swap(effects):
                bo = eo if eo > 0 else d / 2
                add(bass_l, self.beats_to_ms(ts + bo, bpm), "Bass Swap")
            add(end_l, self.beats_to_ms(end_beat, bpm), "Trans End")

        def add_incoming(trans, prep_l, start_l, bass_l, end_l):
            d = trans["duration_beats"]
            effects = trans.get("effects", [])
            eo = trans.get("effect_offset", 0)
            ts = start_beat
            prep = self.get_prep_beats(d)
            add(prep_l, self.beats_to_ms(ts - prep, bpm), "Prep")
            add(start_l, self.beats_to_ms(ts, bpm), "Trans Start")
            if self.has_bass_swap(effects):
                bo = eo if eo > 0 else d / 2
                add(bass_l, self.beats_to_ms(ts + bo, bpm), "Bass Swap")
            add(end_l, self.beats_to_ms(ts + d, bpm), "Trans End")

        if is_first:
            add("A", self.beats_to_ms(start_beat, bpm), "Play Start")
            if outgoing_trans:
                add_outgoing(outgoing_trans, "B", "C", "D", "E")
        else:
            if incoming_trans:
                add_incoming(incoming_trans, "B", "C", "D", "E")
            if outgoing_trans:
                add_outgoing(outgoing_trans, "A", "F", "G", "H")

        return cues

    def import_mix(self, json_data: Dict) -> Dict:
        """Orchestrate the full import process.

        Returns a report dict with matched/unmatched/created tracks and actions taken.
        """
        metadata = json_data["metadata"]
        tracks = json_data["tracks"]
        transitions = json_data.get("transitions", [])
        mix_name = metadata["name"]

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
            "cues_written": 0,
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

        # Step 3b: Write hot cue points (or preview for dry run)
        for track, content in matched_contents:
            if content is None:
                continue

            pos = track["position"]
            is_first = pos == 1

            outgoing = trans_by_num.get(pos)
            incoming = trans_by_num.get(pos - 1)

            cue_preview = self.preview_track_cues(track, incoming, outgoing, is_first)

            if not self.dry_run:
                self.set_track_cues(content, track, incoming, outgoing, is_first)

            report["cues_written"] += 1

            # Attach preview to report entries
            for entry_list in (report["matched"], report["created"]):
                for m in entry_list:
                    if m["position"] == pos:
                        m["cues"] = cue_preview

        # Step 4: Commit
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
        print(f"Hot cues written to {report['cues_written']} tracks.")
    else:
        if unmatched > 0 and any(u.get("beatport_id") for u in report["unmatched"]):
            print(f"\n{unmatched} tracks will be created in rekordbox on actual run.")
        print(f"\nNo changes made (dry run).")

    print(f"{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Import DJ Studio mix JSON into rekordbox database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s mix.json --dry-run    Preview what would be imported
  %(prog)s mix.json              Import mix into rekordbox

Input JSON is generated by:
  python3 get_mix_info.py "Mix Name" -o mix.json

IMPORTANT: Close rekordbox before running this script!
        """,
    )

    parser.add_argument("json_file", help="Path to mix JSON file (from get_mix_info.py)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing to DB",
    )

    args = parser.parse_args()

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

    importer = RekordboxImporter(dry_run=args.dry_run)
    try:
        report = importer.import_mix(json_data)
        print_report(report, args.dry_run)
    finally:
        importer.close()


if __name__ == "__main__":
    main()
