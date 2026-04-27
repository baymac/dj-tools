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

    def test_key_1(self, extractor):
        assert extractor._get_camelot_key(1) == "8B"

    def test_key_13(self, extractor):
        assert extractor._get_camelot_key(13) == "5A"

    def test_max_key(self, extractor):
        assert extractor._get_camelot_key(24) == "10A"

    def test_all_minor_keys(self, extractor):
        minor = {
            1: "8B", 2: "3B", 3: "10B", 4: "5B", 5: "12B", 6: "7B",
            7: "2B", 8: "9B", 9: "4B", 10: "11B", 11: "6B", 12: "1B",
        }
        for num, expected in minor.items():
            assert extractor._get_camelot_key(num) == expected

    def test_all_major_keys(self, extractor):
        major = {
            13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A",
            19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A", 24: "10A",
        }
        for num, expected in major.items():
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
