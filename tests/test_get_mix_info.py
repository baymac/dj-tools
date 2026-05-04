import pytest

from djstudio.extractor import DJStudioMixExtractor


@pytest.fixture
def extractor():
    # Instantiates without loading any files (paths don't exist in test env)
    return DJStudioMixExtractor()


# ── _get_camelot_key ──────────────────────────────────────────────────────────

class TestGetCamelotKey:
    def test_none_returns_na(self, extractor):
        assert extractor._get_camelot_key(None) == "N/A"

    def test_na_string_returns_na(self, extractor):
        assert extractor._get_camelot_key("N/A") == "N/A"

    def test_key_0(self, extractor):
        assert extractor._get_camelot_key(0) == "8B"

    def test_key_12(self, extractor):
        assert extractor._get_camelot_key(12) == "8A"

    def test_max_key(self, extractor):
        assert extractor._get_camelot_key(23) == "1A"

    def test_all_b_keys(self, extractor):
        b_keys = {
            0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
            6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
        }
        for num, expected in b_keys.items():
            assert extractor._get_camelot_key(num) == expected

    def test_all_a_keys(self, extractor):
        a_keys = {
            12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
            18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
        }
        for num, expected in a_keys.items():
            assert extractor._get_camelot_key(num) == expected

    def test_unknown_key_returns_string(self, extractor):
        assert extractor._get_camelot_key(99) == "99"


# ── _format_duration ──────────────────────────────────────────────────────────

class TestFormatDuration:
    def test_zero(self, extractor):
        assert extractor._format_duration(0) == "0:00"

    def test_one_minute(self, extractor):
        assert extractor._format_duration(60) == "1:00"

    def test_65_seconds(self, extractor):
        assert extractor._format_duration(65) == "1:05"

    def test_90_seconds(self, extractor):
        assert extractor._format_duration(90) == "1:30"

    def test_single_seconds(self, extractor):
        assert extractor._format_duration(9) == "0:09"

    def test_one_hour(self, extractor):
        assert extractor._format_duration(3600) == "60:00"
