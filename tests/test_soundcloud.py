"""Tests for detect/soundcloud.py — pure URL + parser helpers (no yt-dlp / network)."""
from detect.soundcloud import (
    _format_oauth_track,
    clean_url,
    derive_from_url,
    is_set_url,
    parse_artist_title,
)


def test_clean_url_strips_si_and_utm():
    raw = (
        "https://soundcloud.com/capturerecords/capture-radio-089"
        "?si=659b3422ff114f199340ccdd0697d9d0"
        "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
    )
    assert clean_url(raw) == "https://soundcloud.com/capturerecords/capture-radio-089"


def test_clean_url_preserves_non_tracking_query():
    raw = "https://soundcloud.com/dj/set?in=playlist&utm_source=clipboard"
    assert clean_url(raw) == "https://soundcloud.com/dj/set?in=playlist"


def test_clean_url_passthrough_when_no_query():
    raw = "https://soundcloud.com/dj/set"
    assert clean_url(raw) == raw


def test_clean_url_idempotent():
    raw = "https://soundcloud.com/dj/set?si=abc"
    once = clean_url(raw)
    twice = clean_url(once)
    assert once == twice == "https://soundcloud.com/dj/set"


def test_is_set_url_detects_sets():
    assert is_set_url("https://soundcloud.com/soundcloud-the-peak/sets/level-up-edm-next") is True


def test_is_set_url_false_for_single_track():
    assert is_set_url("https://soundcloud.com/capturerecords/capture-radio-089") is False


def test_is_set_url_ignores_query_string():
    assert is_set_url("https://soundcloud.com/dj/single-mix?in=abc/sets/foo") is False


def test_parse_artist_title_with_hyphen():
    assert parse_artist_title("Daft Punk - Around the World") == ("Daft Punk", "Around the World")


def test_parse_artist_title_with_em_dash():
    assert parse_artist_title("Sander van Doorn – Inside This Room") == (
        "Sander van Doorn", "Inside This Room"
    )


def test_parse_artist_title_first_separator_only():
    # 'Track Name - Original Mix' should stay intact in the title side.
    assert parse_artist_title("Adam Beyer - Love Within - Original Mix") == (
        "Adam Beyer", "Love Within - Original Mix"
    )


def test_parse_artist_title_no_separator_uses_uploader():
    assert parse_artist_title("Some Track Title", "Capture Records") == (
        "Capture Records", "Some Track Title"
    )


def test_parse_artist_title_empty_uses_unknown():
    assert parse_artist_title("", "") == ("Unknown Artist", "Unknown Title")


def test_derive_from_url_uploader_and_slug():
    assert derive_from_url("https://soundcloud.com/idemimusic/idemi-reflections") == (
        "Idemimusic", "Idemi Reflections"
    )


def test_derive_from_url_underscore_handle():
    assert derive_from_url("https://soundcloud.com/v_nss_a/lets-have-a-kiki") == (
        "V Nss A", "Lets Have A Kiki"
    )


def test_derive_from_url_short_path_falls_back():
    assert derive_from_url("https://soundcloud.com/onlyone") == ("Unknown Artist", "Unknown Title")


def test_format_oauth_track_with_dash_title():
    api = {
        "id": 12345,
        "title": "Daft Punk - Around the World",
        "user": {"username": "daftpunk"},
        "duration": 248_000,
        "permalink_url": "https://soundcloud.com/daftpunk/around-the-world",
    }
    assert _format_oauth_track(api, position=3) == {
        "position": 3,
        "artist": "Daft Punk",
        "title": "Around the World",
        "source_url": "https://soundcloud.com/daftpunk/around-the-world",
        "duration": 248,
    }


def test_format_oauth_track_no_dash_uses_uploader():
    api = {
        "id": 1,
        "title": "Reflections",
        "user": {"username": "IDEMI"},
        "duration": 187_319,
        "permalink_url": "https://soundcloud.com/idemimusic/idemi-reflections",
    }
    assert _format_oauth_track(api, position=1) == {
        "position": 1,
        "artist": "IDEMI",
        "title": "Reflections",
        "source_url": "https://soundcloud.com/idemimusic/idemi-reflections",
        "duration": 187,
    }


def test_format_oauth_track_handles_missing_fields():
    out = _format_oauth_track({"id": 1}, position=1)
    assert out["artist"] == "Unknown Artist"
    assert out["title"] == "Unknown Title"
    assert out["duration"] == 0
    assert out["source_url"] == ""
