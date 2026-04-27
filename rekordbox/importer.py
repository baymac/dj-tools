"""RekordboxImporter — orchestrates Pass 1 (tracks/playlist/effects) and Pass 2 (cues)."""

import datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables

from .backup import backup_db
from .constants import COMMENT_EFFECTS, CUE_KIND
from .cues import (
    beats_to_ms,
    has_bass_swap,
    prep_bars_for,
    snap_to_beatgrid,
    snapped_beats_to_ms,
)


class RekordboxImporter:
    """Import DJ Studio mixes into rekordbox database."""

    def __init__(self, dry_run: bool = False, cues_only: bool = False, snap: bool = True):
        self.dry_run = dry_run
        self.cues_only = cues_only
        self.snap = snap
        self.db = Rekordbox6Database()
        self._device = None
        self._key_cache = {}

    def close(self):
        if self.db is not None:
            self.db.close()

    @property
    def device(self):
        if self._device is None:
            self._device = self.db.get_device().first()
        return self._device

    # ── Track lookup / creation ──────────────────────────────────────────────

    def find_track_by_beatport_id(self, beatport_id: str) -> Optional[tables.DjmdContent]:
        folder_path = f"/v4/catalog/tracks/{beatport_id}/"
        return self.db.get_content(FolderPath=folder_path).first()

    def get_or_create_artist(self, name: str) -> tables.DjmdArtist:
        existing = self.db.get_artist(Name=name).first()
        return existing if existing else self.db.add_artist(name=name)

    def get_or_create_genre(self, name: str) -> tables.DjmdGenre:
        existing = self.db.get_genre(Name=name).first()
        return existing if existing else self.db.add_genre(name=name)

    def get_key_id(self, camelot_key: str) -> Optional[str]:
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
        """Create a Beatport streaming entry (FileType=20) in the rekordbox DB."""
        folder_path = f"/v4/catalog/tracks/{beatport_id}/"

        content_id = str(self.db.generate_unused_id(tables.DjmdContent))
        file_id = str(self.db.generate_unused_id(tables.DjmdContent, id_field_name="rb_file_id"))
        content_uuid = str(uuid4())

        artist = self.get_or_create_artist(track_info.get("artist", "Unknown"))

        genre_id = None
        genre_name = track_info.get("genre", "")
        if genre_name:
            genre_id = self.get_or_create_genre(genre_name).ID

        key_id = self.get_key_id(track_info.get("key", ""))

        # Rekordbox stores BPM as int * 100
        bpm_raw = track_info.get("bpm", 0)
        bpm = int(bpm_raw * 100) if bpm_raw else 0

        length = int(track_info.get("duration", 0))

        # Split "Title (Extended Mix)" → Title + Subtitle
        title = track_info.get("title", "Unknown")
        subtitle = ""
        if "(" in title and title.endswith(")"):
            paren_start = title.rfind("(")
            subtitle = title[paren_start + 1 : -1].strip()
            title = title[:paren_start].strip()

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
            Commnt="",
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

    # ── Playlist ─────────────────────────────────────────────────────────────

    def create_playlist(self, name: str) -> tables.DjmdPlaylist:
        return self.db.create_playlist(name=name)

    def add_track_to_playlist(
        self,
        playlist: tables.DjmdPlaylist,
        content: tables.DjmdContent,
        position: int,
    ):
        self.db.add_to_playlist(playlist=playlist, content=content, track_no=position)

    # ── Comment field with transition effect names ───────────────────────────

    def set_track_effects(
        self,
        content: tables.DjmdContent,
        outgoing_effects: Optional[List[str]],
        incoming_effects: Optional[List[str]],
    ):
        """Write 'Trans out: ... | Trans in: ...' into the Commnt field."""
        parts = []
        if outgoing_effects:
            filtered = [e for e in outgoing_effects if e in COMMENT_EFFECTS]
            if filtered:
                clean = [e.replace("AE_", "") for e in filtered]
                parts.append(f"Trans out: {', '.join(clean)}")
        if incoming_effects:
            filtered = [e for e in incoming_effects if e in COMMENT_EFFECTS]
            if filtered:
                clean = [e.replace("AE_", "") for e in filtered]
                parts.append(f"Trans in: {', '.join(clean)}")
        content.Commnt = " | ".join(parts)

    # ── Cue helpers (test-facing static methods preserved) ───────────────────

    @staticmethod
    def beats_to_ms(beat: float, bpm: float) -> int:
        return beats_to_ms(beat, bpm)

    @staticmethod
    def snap_to_beatgrid(ms: int, beat_times_ms: List[float]) -> int:
        return snap_to_beatgrid(ms, beat_times_ms)

    @staticmethod
    def has_bass_swap(effects: List[str]) -> bool:
        return has_bass_swap(effects)

    def snapped_beats_to_ms(self, beat: float, bpm: float, beat_times_ms: Optional[List[float]]) -> int:
        return snapped_beats_to_ms(beat, bpm, beat_times_ms, self.snap)

    def get_beatgrid(self, content: "tables.DjmdContent") -> Optional[List[float]]:
        """Read ANLZ PQTZ tag → downbeat (beat 1) times in ms.

        Filters to beat 1 of each bar so cue snapping lands on bar boundaries.
        Returns None if no ANLZ files exist or the data is corrupt.
        """
        try:
            anlz_files = self.db.read_anlz_files(content)
            for anlz_file in anlz_files.values():
                if "PQTZ" in anlz_file:
                    beats, bpms, times = anlz_file.get("PQTZ")
                    return sorted(
                        float(t) * 1000.0
                        for b, t in zip(beats, times)
                        if int(b) == 1
                    )
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            pass
        return None

    # ── Hot cue writing ──────────────────────────────────────────────────────

    def clear_hot_cues(self, content: tables.DjmdContent):
        for cue in self.db.get_cue(ContentID=content.ID).all():
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
        now = datetime.datetime.now()
        cue = tables.DjmdCue.create(
            ID=str(uuid4()),
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
        """Write the prep/start/(bass)/end cues for one transition."""
        duration_beats = transition["duration_beats"]
        effects = transition.get("effects", [])
        effect_offset = transition.get("effect_offset", 0)
        prep_beats = prep_bars_for(genre, duration_beats) * 4

        self.add_hot_cue(
            content, cue_prep,
            self.snapped_beats_to_ms(anchor_beat - prep_beats, bpm, beat_times_ms), "Prep",
        )
        self.add_hot_cue(
            content, cue_start,
            self.snapped_beats_to_ms(anchor_beat, bpm, beat_times_ms), "Trans Start",
        )
        if has_bass_swap(effects):
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
        """Cue layout: A-D = incoming, E-H = outgoing (prep/start/bass/end)."""
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
        """Same logic as set_track_cues but returns a list rather than writing."""
        bpm = track.get("bpm", 0)
        if not bpm or bpm <= 0:
            return []

        genre = track.get("genre")
        start_beat = track.get("start_beat", 0)
        end_beat = track.get("end_beat", 0)
        snapped = beat_times_ms is not None and self.snap
        cues: List[Dict] = []

        def add(letter, ms, label):
            cues.append({"letter": letter, "ms": max(0, ms), "label": label, "snapped": snapped})

        def add_transition(anchor, trans, prep_l, start_l, bass_l, end_l):
            d = trans["duration_beats"]
            effects = trans.get("effects", [])
            eo = trans.get("effect_offset", 0)
            prep_beats = prep_bars_for(genre, d) * 4
            add(prep_l, self.snapped_beats_to_ms(anchor - prep_beats, bpm, beat_times_ms), "Prep")
            add(start_l, self.snapped_beats_to_ms(anchor, bpm, beat_times_ms), "Trans Start")
            if has_bass_swap(effects):
                bo = eo if eo > 0 else d / 2
                add(bass_l, self.snapped_beats_to_ms(anchor + bo, bpm, beat_times_ms), "Bass Swap")
            add(end_l, self.snapped_beats_to_ms(anchor + d, bpm, beat_times_ms), "Trans End")

        if incoming_trans:
            add_transition(start_beat, incoming_trans, "A", "B", "C", "D")
        if outgoing_trans:
            add_transition(end_beat, outgoing_trans, "E", "F", "G", "H")
        return cues

    # ── Top-level passes ─────────────────────────────────────────────────────

    @staticmethod
    def _anlz_path_for_uuid(uuid: str) -> str:
        """Derive the expected ANLZ .DAT path from a DjmdContent UUID.

        Rekordbox stores analysis files at PIONEER/USBANLZ/{uuid[:3]}/{uuid[3:]}/ANLZ0000.DAT
        relative to the share/ directory. UUID is assigned when the content row is created,
        so we can predict the path before rekordbox runs analysis.
        """
        return f"/PIONEER/USBANLZ/{uuid[:3]}/{uuid[3:]}/ANLZ0000.DAT"

    def import_mix(self, json_data: Dict) -> Dict:
        """Pass 1: match/create tracks, build playlist, write effects to Commnt."""
        metadata = json_data["metadata"]
        tracks = json_data["tracks"]
        transitions = json_data.get("transitions", [])
        mix_name = metadata["name"]

        if not self.dry_run:
            bp = backup_db(mix_name)
            if not bp:
                raise RuntimeError("Could not create backup of master.db — aborting. Is rekordbox installed?")
            print(f"Backup: {bp.name}")

        trans_by_num = {t["number"]: t for t in transitions}

        report = {
            "mix_name": mix_name,
            "total_tracks": len(tracks),
            "matched": [],
            "created": [],
            "unmatched": [],
            "playlist_created": False,
            "effects_written": 0,
            "anlz_manifest": [],
        }

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

            if content is not None:
                uuid = getattr(content, "UUID", None)
                dat_path = self._anlz_path_for_uuid(uuid) if uuid else None
                report["anlz_manifest"].append({
                    "position": track["position"],
                    "title": track.get("title", "?"),
                    "dat_path": dat_path,
                })

            matched_contents.append((track, content))

        playlist = None
        if not self.dry_run and any(c is not None for _, c in matched_contents):
            playlist = self.create_playlist(mix_name)
            report["playlist_created"] = True

        for track, content in matched_contents:
            if content is None:
                continue
            pos = track["position"]
            if playlist is not None:
                self.add_track_to_playlist(playlist, content, pos)

            outgoing = trans_by_num.get(pos)
            incoming = trans_by_num.get(pos - 1)
            outgoing_effects = outgoing["effects"] if outgoing else None
            incoming_effects = incoming["effects"] if incoming else None

            if outgoing_effects or incoming_effects:
                if self.dry_run:
                    clean_out = (
                        [e.replace("AE_", "") for e in outgoing_effects if e in COMMENT_EFFECTS]
                        if outgoing_effects else []
                    )
                    clean_in = (
                        [e.replace("AE_", "") for e in incoming_effects if e in COMMENT_EFFECTS]
                        if incoming_effects else []
                    )
                    for m in report["matched"]:
                        if m["position"] == pos:
                            m["effects_out"] = clean_out
                            m["effects_in"] = clean_in
                else:
                    self.set_track_effects(content, outgoing_effects, incoming_effects)
                report["effects_written"] += 1

        if not self.dry_run:
            self.db.commit()
        return report

    def import_cues_only(self, json_data: Dict) -> Dict:
        """Pass 2: read ANLZ beatgrids, snap cue points, write hot cues."""
        tracks = json_data["tracks"]
        transitions = json_data.get("transitions", [])
        mix_name = json_data["metadata"]["name"]

        if not self.dry_run:
            bp = backup_db(mix_name + "_cues")
            if not bp:
                raise RuntimeError("Could not create backup of master.db — aborting. Is rekordbox installed?")
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

            beat_times_ms = self.get_beatgrid(content)
            snapped = beat_times_ms is not None and self.snap

            if beat_times_ms is None:
                report["no_beatgrid"].append(track.get("title", "?"))

            cue_preview = self.preview_track_cues(
                track, incoming, outgoing, is_first, beat_times_ms=beat_times_ms,
            )

            if not self.dry_run:
                self.set_track_cues(
                    content, track, incoming, outgoing, is_first, beat_times_ms=beat_times_ms,
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
