"""Pure cue math: beat→ms conversion, beatgrid snapping, prep bar calculation."""

import bisect
from typing import List, Optional

from .constants import BASS_SWAP_EFFECTS, GENRE_PREP_BARS, PREP_BARS


def beats_to_ms(beat: float, bpm: float) -> int:
    """Convert a beat position to milliseconds, snapped to the nearest whole beat.

    DJ Studio's beat grid doesn't always align with rekordbox's, so beat positions
    may be fractional. Rounding to the nearest integer beat lands cues on exact
    beat boundaries even before rekordbox analyzes the track.
    """
    if bpm <= 0:
        return 0
    return int(round(beat) * 60000.0 / bpm)


def snap_to_beatgrid(ms: int, beat_times_ms: List[float]) -> int:
    """Snap a millisecond position to the nearest beat in rekordbox's grid."""
    if not beat_times_ms:
        return ms
    idx = bisect.bisect_left(beat_times_ms, ms)
    if idx == 0:
        return int(beat_times_ms[0])
    if idx >= len(beat_times_ms):
        return ms  # cue is past the last known beat; keep raw position
    before = beat_times_ms[idx - 1]
    after = beat_times_ms[idx]
    if (ms - before) <= (after - ms):
        return int(before)
    return int(after)


def snapped_beats_to_ms(
    beat: float,
    bpm: float,
    beat_times_ms: Optional[List[float]],
    snap: bool,
) -> int:
    """Convert beat to ms, optionally snapping to rekordbox's analyzed beatgrid."""
    raw_ms = beats_to_ms(beat, bpm)
    if beat_times_ms is not None and snap:
        return snap_to_beatgrid(raw_ms, beat_times_ms)
    return raw_ms


def has_bass_swap(effects: List[str]) -> bool:
    """True if any bass-swap effect is in the transition."""
    return bool(BASS_SWAP_EFFECTS.intersection(effects))


def prep_bars_for(genre: Optional[str], duration_beats: float) -> int:
    """Bars before transition start, tuned to genre and capped at half the transition.

    The cap ensures the prep cue never lands before the previous transition's end.
    """
    bars = PREP_BARS
    if genre:
        g = genre.lower()
        for keyword, mapped in GENRE_PREP_BARS.items():
            if keyword in g:
                bars = mapped
                break
    max_bars = max(2, int(duration_beats / 8))
    return min(bars, max_bars)
