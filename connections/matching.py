"""Fuzzy matching between Apple Music tracks and Beatport search results."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

MATCH_THRESHOLD = 0.72

_BARE_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?)\s+\S.*", re.I)
# Matches a parenthetical/bracket block that contains the word "remix" — used to detect
# remix versions so "X (Someone Remix)" is never silently matched against plain "X".
_REMIX_TAG_RE = re.compile(r"[\(\[][^\)\]]*\bremix\b[^\)\]]*[\)\]]", re.I)
# Generic remix tags are version labels, not specific remixes.
# Heuristic: remixer names need 2+ words (e.g. "John Summit Remix").
# A single prefix word — year, adjective, style, or one-word alias — is treated as generic.
# Covers: [Remix], (2022 Remix), (Albanian Remix), (Extended Remix), (Alok Remix), etc.
_GENERIC_REMIX_RE = re.compile(r"^(\d{4}|\w+)?\s*remix$", re.I)


def _normalise(s: str) -> str:
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)  # drop all (...) / [...] blocks
    s = _BARE_FEAT_RE.sub("", s)                    # drop bare "feat. X" (Beatport style)
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _remix_tag(s: str) -> str:
    """Normalised remix tag from title, or '' if generic/absent."""
    m = _REMIX_TAG_RE.search(s)
    if not m:
        return ""
    tag = re.sub(r"[^\w\s]", " ", m.group(0).lower()).strip()
    if _GENERIC_REMIX_RE.match(tag):
        return ""
    return tag


def _title_score(a: str, b: str, bp_artist: str = "") -> float:
    ta, tb = _remix_tag(a), _remix_tag(b)
    if ta != tb:
        # Allow when AM names the remixer in the title but Beatport lists them as an
        # artist instead — e.g. "X (Ben Böhmer Remix)" vs "X" by "Monolink, Ben Böhmer".
        if ta and not tb and bp_artist:
            m = _REMIX_TAG_RE.search(a)
            if m:
                raw = re.sub(r"[\(\[\)\]]", "", m.group(0))
                raw = re.sub(r"\s*\b(?:re)?mix\b\s*", " ", raw, flags=re.I)
                remixer_parts = [
                    _normalise(p) for p in re.split(r"\s+[x&]\s+|,\s*", raw, flags=re.I)
                    if _normalise(p)
                ]
            else:
                remixer_parts = []
            bp_norm = _normalise(bp_artist)
            if remixer_parts and all(p in bp_norm for p in remixer_parts):
                pass
            else:
                return 0.0
        else:
            return 0.0
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _artist_score(a: str, b: str) -> float:
    def tokens(s: str) -> set[str]:
        return {_normalise(p) for p in re.split(r"[,;&/]+", s) if _normalise(p)}

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    if tb <= ta or ta <= tb:
        return 1.0
    overlap = len(ta & tb) / max(len(ta), len(tb))
    return overlap if overlap > 0 else SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


# Detects "Actual Artist – Actual Title" embedded in the title field.
# Common in DJ show tracks where the host is the AM artist but the real
# artist–title pair is encoded in the title with an en/em dash.
_EMBEDDED_ARTIST_RE = re.compile(r"^(.+?)\s+[–—]\s+(.+)$")

_REMIX_STRIP_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:remix|mix|edit|rework|dub|bootleg|mashup|version|vip|flip)\b[^\)\]]*[\)\]]",
    re.I,
)


def strip_remix(title: str) -> Optional[str]:
    """Return title with remix/mix/edit tag removed, or None if no such tag exists."""
    stripped = _REMIX_STRIP_RE.sub("", title).strip()
    return stripped if stripped != title else None


def search_query(name: str) -> str:
    """Simplify a track title for Beatport search — strip feat and bracket noise,
    keep the remix/edit name so the right version ranks higher."""
    q = re.sub(r"\s*[\(\[]feat\.?[^\)\]]*[\)\]]", "", name, flags=re.I)
    q = _BARE_FEAT_RE.sub("", q)
    q = re.sub(r"[\(\[\)\]]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def combined_score(am_name: str, am_artist: str, bp_name: str, bp_artist: str) -> float:
    return 0.6 * _title_score(am_name, bp_name, bp_artist) + 0.4 * _artist_score(am_artist, bp_artist)


def best_match(
    am_name: str,
    am_artist: str,
    candidates: list[dict],
    threshold: float = MATCH_THRESHOLD,
) -> tuple[Optional[dict], float]:
    """Return (best_candidate, score) if score >= threshold, else (None, best_score)."""
    best: Optional[dict] = None
    best_score = 0.0
    for c in candidates:
        bp_name = c.get("name", "")
        all_bp_artists = (
            [a.get("name", "") for a in c.get("artists", [])] +
            [r.get("name", "") for r in c.get("remixers", [])]
        )
        bp_artists = ", ".join(all_bp_artists)
        score = combined_score(am_name, am_artist, bp_name, bp_artists)
        # If the title encodes "Actual Artist – Actual Title", try that split too.
        m = _EMBEDDED_ARTIST_RE.match(am_name)
        if m:
            score = max(score, combined_score(m.group(2), m.group(1), bp_name, bp_artists))
        if score > best_score:
            best_score = score
            best = c
    if best_score >= threshold:
        return best, best_score
    return None, best_score
