"""DJ Studio numeric key → Camelot notation."""

from typing import Optional


CAMELOT_MAP = {
    1: "8B",  2: "3B",  3: "10B", 4: "5B",  5: "12B", 6: "7B",
    7: "2B",  8: "9B",  9: "4B",  10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A",
    19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A", 24: "10A",
}


def get_camelot_key(key_num: Optional[int]) -> str:
    """Convert DJ Studio numeric key (1-24) to Camelot notation, e.g. 11 → '6B'."""
    if key_num is None or key_num == "N/A":
        return "N/A"
    return CAMELOT_MAP.get(key_num, str(key_num))
