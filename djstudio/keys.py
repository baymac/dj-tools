"""DJ Studio numeric key → Camelot notation.

DJ Studio stores `mikKey` / `camelotKey` as a 0-23 integer (Circle of Fifths order),
verified empirically against real audio-library-table entries. Earlier code in this
repo used a 1-24 map that was off by one slot — that's been corrected to match.
"""

from typing import Optional


CAMELOT_MAP = {
    0: "8B",  1: "3B",  2: "10B", 3: "5B",  4: "12B", 5: "7B",
    6: "2B",  7: "9B",  8: "4B",  9: "11B", 10: "6B", 11: "1B",
    12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
    18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
}


def get_camelot_key(key_num: Optional[int]) -> str:
    """Convert DJ Studio numeric key (0-23) to Camelot notation, e.g. 10 → '6B'."""
    if key_num is None or key_num == "N/A":
        return "N/A"
    return CAMELOT_MAP.get(key_num, str(key_num))
