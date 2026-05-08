"""Tests for pure functions in helpers/download_course.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "helpers"))

from download_course import (
    LessonType,
    _cookies_to_netscape,
    _sanitize,
    classify,
)


class TestSanitize:
    def test_replaces_spaces_with_underscores(self):
        assert _sanitize("Hello World") == "Hello_World"

    def test_strips_forbidden_chars(self):
        assert "/" not in _sanitize("foo/bar")
        assert ":" not in _sanitize("foo:bar")
        assert '"' not in _sanitize('foo"bar')

    def test_truncates_to_60_chars(self):
        long = "a" * 100
        assert len(_sanitize(long)) <= 60

    def test_empty_string_returns_lesson(self):
        assert _sanitize("") == "lesson"
        assert _sanitize("   ") == "lesson"

    def test_unicode_passthrough(self):
        result = _sanitize("DJ Técnique")
        assert len(result) > 0


class TestCookiesToNetscape:
    def test_produces_header_line(self):
        assert _cookies_to_netscape([]).startswith("# Netscape HTTP Cookie File")

    def test_formats_cookie_row(self):
        out = _cookies_to_netscape([{
            "domain": "example.com", "path": "/", "secure": True,
            "expires": 1700000000, "name": "session", "value": "abc123",
        }])
        assert ".example.com" in out and "session" in out and "abc123" in out

    def test_prepends_dot_to_domain(self):
        out = _cookies_to_netscape([{"domain": "example.com", "name": "x", "value": "y", "path": "/"}])
        assert ".example.com" in out

    def test_does_not_double_prepend_dot(self):
        out = _cookies_to_netscape([{"domain": ".example.com", "name": "x", "value": "y", "path": "/"}])
        assert "..example.com" not in out

    def test_handles_negative_expiry(self):
        out = _cookies_to_netscape([{"domain": "a.com", "name": "x", "value": "y", "path": "/", "expires": -1}])
        assert "\t0\t" in out


class TestClassify:
    def test_locked(self):
        sigs = {"is_locked": True, "content_text": "Lesson locked. Unlock by ..."}
        assert classify(sigs, "Anything") == LessonType.LOCKED

    def test_circle_video(self):
        sigs = {
            "is_locked": False,
            "sources": [{
                "src": "https://cdn-media.circle.so/.../hls/playlist.m3u8",
                "type": "application/x-mpegURL",
            }],
            "iframes": [], "radios": 0, "forms": 0, "body_text_len": 500,
        }
        assert classify(sigs, "Welcome") == LessonType.VIDEO_CIRCLE

    def test_dyntube_iframe(self):
        sigs = {
            "is_locked": False, "sources": [],
            "iframes": [{"src": "https://videos.dyntube.com/iframes/abc"}],
            "radios": 0, "forms": 0, "body_text_len": 500,
        }
        assert classify(sigs, "Lesson 1: Whatever") == LessonType.VIDEO_DYNTUBE

    def test_quiz_form_with_radios(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 4, "forms": 1, "body_text_len": 500,
        }
        assert classify(sigs, "Quiz: Setup") == LessonType.QUIZ

    def test_exercise_by_title(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 500, "download_links": [],
        }
        assert classify(sigs, "Exercise: Build your record bag") == LessonType.EXERCISE

    def test_exercise_numbered(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 500, "download_links": [],
        }
        assert classify(sigs, "Exercise 1: The downbeat count") == LessonType.EXERCISE
        assert classify(sigs, "Exercise 12: Saving / rescuing a mix") == LessonType.EXERCISE

    def test_exercise_by_download_links(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 500,
            "download_links": [{"name": "stems.zip", "href": "https://x/y.zip"}],
        }
        assert classify(sigs, "Random title") == LessonType.EXERCISE

    def test_guide_by_title(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 500, "download_links": [],
        }
        assert classify(sigs, "Guide: Build your setup") == LessonType.GUIDE

    def test_content_fallback(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 500, "download_links": [],
        }
        assert classify(sigs, "Welcome & Intro") == LessonType.CONTENT

    def test_unknown_when_empty(self):
        sigs = {
            "is_locked": False, "sources": [], "iframes": [],
            "radios": 0, "forms": 0, "body_text_len": 0, "download_links": [],
        }
        assert classify(sigs, "") == LessonType.UNKNOWN

    def test_locked_takes_precedence_over_video(self):
        sigs = {
            "is_locked": True, "content_text": "Lesson locked",
            "sources": [{"src": "circle.so/.m3u8", "type": "application/x-mpegURL"}],
            "iframes": [{"src": "dyntube.com/x"}],
        }
        assert classify(sigs, "Lesson 1: X") == LessonType.LOCKED
