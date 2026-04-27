import pytest
from unittest.mock import MagicMock, patch

from rekordbox.importer import RekordboxImporter
from rekordbox.display import _fmt_ms


@pytest.fixture
def importer():
    with patch("rekordbox.importer.Rekordbox6Database"):
        return RekordboxImporter(dry_run=True, snap=True)


# ── beats_to_ms ──────────────────────────────────────────────────────────────

class TestBeatsToMs:
    def test_one_minute(self):
        assert RekordboxImporter.beats_to_ms(128, 128) == 60000

    def test_zero_beat(self):
        assert RekordboxImporter.beats_to_ms(0, 128) == 0

    def test_zero_bpm_returns_zero(self):
        assert RekordboxImporter.beats_to_ms(64, 0) == 0

    def test_half_minute(self):
        assert RekordboxImporter.beats_to_ms(64, 128) == 30000

    def test_fractional_beat_rounds_down(self):
        # round(128.3) == 128
        assert RekordboxImporter.beats_to_ms(128.3, 128) == 60000

    def test_fractional_beat_rounds_up(self):
        # round(128.7) == 129
        assert RekordboxImporter.beats_to_ms(128.7, 128) == int(129 * 60000.0 / 128)

    def test_negative_beat(self):
        assert RekordboxImporter.beats_to_ms(-16, 128) == int(-16 * 60000.0 / 128)


# ── snap_to_beatgrid ──────────────────────────────────────────────────────────

class TestSnapToBeatgrid:
    def test_empty_grid_returns_input(self):
        assert RekordboxImporter.snap_to_beatgrid(5000, []) == 5000

    def test_before_first_beat(self):
        assert RekordboxImporter.snap_to_beatgrid(0, [1000.0, 2000.0, 3000.0]) == 1000

    def test_after_last_beat(self):
        # Cue past the last known beat: keep raw position, don't clamp to last beat
        assert RekordboxImporter.snap_to_beatgrid(9999, [1000.0, 2000.0, 3000.0]) == 9999

    def test_exact_match(self):
        assert RekordboxImporter.snap_to_beatgrid(2000, [1000.0, 2000.0, 3000.0]) == 2000

    def test_closer_to_left(self):
        assert RekordboxImporter.snap_to_beatgrid(1300, [1000.0, 2000.0]) == 1000

    def test_closer_to_right(self):
        assert RekordboxImporter.snap_to_beatgrid(1700, [1000.0, 2000.0]) == 2000

    def test_equidistant_picks_left(self):
        assert RekordboxImporter.snap_to_beatgrid(1500, [1000.0, 2000.0]) == 1000

    def test_single_beat(self):
        # Past the only known beat: keep raw position
        assert RekordboxImporter.snap_to_beatgrid(5000, [3000.0]) == 5000


# ── has_bass_swap ─────────────────────────────────────────────────────────────

class TestHasBassSwap:
    def test_bass_swap(self):
        assert RekordboxImporter.has_bass_swap(["AE_Bass_Swap"]) is True

    def test_bass_swap_fade(self):
        assert RekordboxImporter.has_bass_swap(["AE_Bass_SwapFade"]) is True

    def test_bass_crossfade(self):
        assert RekordboxImporter.has_bass_swap(["AE_Bass_CrossFade"]) is True

    def test_no_bass_effects(self):
        assert RekordboxImporter.has_bass_swap(["AE_CrossFade", "AE_FadeIn"]) is False

    def test_empty(self):
        assert RekordboxImporter.has_bass_swap([]) is False

    def test_mixed_with_bass(self):
        assert RekordboxImporter.has_bass_swap(["AE_CrossFade", "AE_Bass_Swap"]) is True


# ── set_track_effects ─────────────────────────────────────────────────────────

class TestSetTrackEffects:
    def test_outgoing_only(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, ["AE_CrossFade"], None)
        assert content.Commnt == "Trans out: CrossFade"

    def test_incoming_only(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, None, ["AE_CrossFade"])
        assert content.Commnt == "Trans in: CrossFade"

    def test_both_directions(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, ["AE_CrossFade"], ["AE_Bass_CrossFade"])
        assert content.Commnt == "Trans out: CrossFade | Trans in: Bass_CrossFade"

    def test_filters_non_comment_effects(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, ["AE_CrossFade", "AE_Filter_LPF"], None)
        assert content.Commnt == "Trans out: CrossFade"

    def test_all_effects_filtered_out(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, ["AE_Filter_LPF"], None)
        assert content.Commnt == ""

    def test_empty_lists(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, [], [])
        assert content.Commnt == ""

    def test_none_both(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, None, None)
        assert content.Commnt == ""

    def test_multiple_effects(self, importer):
        content = MagicMock()
        importer.set_track_effects(content, ["AE_CrossFade", "AE_Bass_CrossFade"], None)
        assert content.Commnt == "Trans out: CrossFade, Bass_CrossFade"


# ── preview_track_cues ────────────────────────────────────────────────────────

class TestPreviewTrackCues:
    def _track(self, bpm=128, start_beat=64, end_beat=320):
        return {"bpm": bpm, "start_beat": start_beat, "end_beat": end_beat}

    def _trans(self, duration_beats=64, effects=None, effect_offset=0):
        return {
            "duration_beats": duration_beats,
            "effects": effects or [],
            "effect_offset": effect_offset,
        }

    def test_no_transitions_returns_empty(self, importer):
        cues = importer.preview_track_cues(self._track(), None, None, False)
        assert cues == []

    def test_zero_bpm_returns_empty(self, importer):
        cues = importer.preview_track_cues(self._track(bpm=0), self._trans(), None, False)
        assert cues == []

    def test_incoming_only_letters(self, importer):
        cues = importer.preview_track_cues(self._track(), self._trans(), None, False)
        assert [c["letter"] for c in cues] == ["A", "B", "D"]

    def test_outgoing_only_letters(self, importer):
        cues = importer.preview_track_cues(self._track(), None, self._trans(), False)
        assert [c["letter"] for c in cues] == ["E", "F", "H"]

    def test_both_no_bass_swap(self, importer):
        cues = importer.preview_track_cues(self._track(), self._trans(), self._trans(), False)
        assert [c["letter"] for c in cues] == ["A", "B", "D", "E", "F", "H"]

    def test_both_with_bass_swap(self, importer):
        trans = self._trans(effects=["AE_Bass_Swap"])
        cues = importer.preview_track_cues(self._track(), trans, trans, False)
        assert [c["letter"] for c in cues] == ["A", "B", "C", "D", "E", "F", "G", "H"]

    def test_incoming_cue_positions(self, importer):
        # start_beat=64, bpm=128, PREP_BARS=8 → prep=32 beats
        track = self._track(bpm=128, start_beat=64)
        cues = importer.preview_track_cues(track, self._trans(duration_beats=64), None, False)
        by_letter = {c["letter"]: c["ms"] for c in cues}

        assert by_letter["A"] == 15000   # (64-32) * 60000/128
        assert by_letter["B"] == 30000   # 64 * 60000/128
        assert by_letter["D"] == 60000   # (64+64) * 60000/128

    def test_outgoing_cue_positions(self, importer):
        track = self._track(bpm=128, end_beat=320)
        cues = importer.preview_track_cues(track, None, self._trans(duration_beats=64), False)
        by_letter = {c["letter"]: c["ms"] for c in cues}

        assert by_letter["E"] == 135000  # (320-32) * 60000/128
        assert by_letter["F"] == 150000  # 320 * 60000/128
        assert by_letter["H"] == 180000  # (320+64) * 60000/128

    def test_bass_swap_uses_effect_offset(self, importer):
        track = self._track(bpm=128, start_beat=64)
        trans = self._trans(effects=["AE_Bass_Swap"], effect_offset=16)
        cues = importer.preview_track_cues(track, trans, None, False)
        by_letter = {c["letter"]: c["ms"] for c in cues}
        assert by_letter["C"] == 37500

    def test_bass_swap_uses_midpoint_when_no_offset(self, importer):
        track = self._track(bpm=128, start_beat=64)
        trans = self._trans(duration_beats=64, effects=["AE_Bass_Swap"], effect_offset=0)
        cues = importer.preview_track_cues(track, trans, None, False)
        by_letter = {c["letter"]: c["ms"] for c in cues}
        assert by_letter["C"] == 45000

    def test_prep_cue_clamps_to_zero(self, importer):
        # start_beat=16, prep=32 → -16 beats → negative ms, clamped to 0
        track = self._track(bpm=128, start_beat=16)
        cues = importer.preview_track_cues(track, self._trans(), None, False)
        by_letter = {c["letter"]: c["ms"] for c in cues}
        assert by_letter["A"] == 0

    def test_snapping_uses_beatgrid(self, importer):
        track = self._track(bpm=128, start_beat=64)
        beat_times = [15000.0, 30000.0, 60000.0]
        cues = importer.preview_track_cues(
            track, self._trans(duration_beats=64), None, False, beat_times_ms=beat_times
        )
        by_letter = {c["letter"]: c["ms"] for c in cues}
        assert by_letter["A"] == 15000
        assert by_letter["B"] == 30000
        assert by_letter["D"] == 60000

    def test_no_snap_ignores_beatgrid(self, importer):
        with patch("rekordbox.importer.Rekordbox6Database"):
            no_snap_importer = RekordboxImporter(dry_run=True, snap=False)
        track = self._track(bpm=128, start_beat=64)
        beat_times = [15500.0, 30500.0, 60500.0]
        cues = no_snap_importer.preview_track_cues(
            track, self._trans(duration_beats=64), None, False, beat_times_ms=beat_times
        )
        by_letter = {c["letter"]: c["ms"] for c in cues}
        assert by_letter["B"] == 30000  # raw, not snapped


# ── _fmt_ms ───────────────────────────────────────────────────────────────────

class TestFmtMs:
    def test_zero(self):
        assert _fmt_ms(0) == "0:00.0"

    def test_one_minute(self):
        assert _fmt_ms(60000) == "1:00.0"

    def test_90_seconds(self):
        assert _fmt_ms(90000) == "1:30.0"

    def test_sub_second(self):
        assert _fmt_ms(500) == "0:00.5"

    def test_two_minutes_15_seconds(self):
        assert _fmt_ms(135500) == "2:15.5"
