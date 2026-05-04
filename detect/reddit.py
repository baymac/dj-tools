"""Reddit post track list fetcher — no audio, pure text parsing."""

from __future__ import annotations

import re
from typing import Optional

import httpx

_USER_AGENT = "dj-tools/1.0 (track-detect; github.com/baymac/dj-tools)"

# Separators between artist and title in a track line
_SEP_RE = re.compile(r"\s+[-–—]\s+")
# Labels in square brackets at end of title: "[Drumcode]" / "[ Kompakt ]"
_LABEL_RE = re.compile(r"\s*\[[^\]]{1,50}\]\s*$")
# Reddit markdown link: [text](url) → keep text
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")
# Bare URL on its own
_URL_RE = re.compile(r"^https?://\S+$")
# Leading position number: "1. " / "2) "
_POS_RE = re.compile(r"^\d+[.)]\s*")
# Reddit quote / header markers
_SKIP_PREFIX_RE = re.compile(r"^[>#*_~`|]")


def _clean_line(line: str) -> str:
    """Expand markdown links, strip labels and leading noise."""
    line = _MD_LINK_RE.sub(r"\1", line)
    line = _POS_RE.sub("", line.strip())
    line = _LABEL_RE.sub("", line)
    return line.strip()


def _parse_line(line: str) -> Optional[dict]:
    """Return {artist, title} for a track line, or None if not a track."""
    if not line or _SKIP_PREFIX_RE.match(line) or _URL_RE.match(line):
        return None
    if len(line) > 250:
        return None

    cleaned = _clean_line(line)
    if not cleaned:
        return None

    m = _SEP_RE.search(cleaned)
    if not m:
        return None

    artist = cleaned[: m.start()].strip()
    title = cleaned[m.end() :].strip()

    # Reject implausible splits
    if not artist or not title:
        return None
    if len(artist) > 120 or len(title) > 180:
        return None
    # Skip lines where the "artist" looks like a markdown/section header word
    if len(artist.split()) > 8:
        return None

    return {"artist": artist, "title": title}


def _extract_from_text(text: str) -> list[dict]:
    """Parse all track lines from a block of text; deduplicate by (artist, title)."""
    tracks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    pos = 0
    for line in text.splitlines():
        track = _parse_line(line.strip())
        if track:
            key = (track["artist"].lower(), track["title"].lower())
            if key not in seen:
                seen.add(key)
                pos += 1
                track["position"] = pos
                tracks.append(track)
    return tracks


def fetch_post(url: str) -> dict:
    """
    Fetch a Reddit post via the public JSON API.
    Returns: {title, selftext, subreddit, author, post_url, top_comment}
    """
    api_url = url.rstrip("/").split("?")[0] + ".json"
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(follow_redirects=True, timeout=30, headers=headers) as client:
        resp = client.get(api_url)
        resp.raise_for_status()
        data = resp.json()

    post_data = data[0]["data"]["children"][0]["data"]

    # Find best comment: prefer stickied/mod, else first substantial top-level
    top_comment = ""
    for child in data[1]["data"]["children"]:
        if child.get("kind") != "t1":
            continue
        c = child["data"]
        body = c.get("body", "")
        if c.get("stickied") or c.get("distinguished") == "moderator":
            top_comment = body
            break
        if not top_comment and len(body) > 80:
            top_comment = body

    return {
        "title": post_data.get("title", ""),
        "selftext": post_data.get("selftext", ""),
        "subreddit": post_data.get("subreddit", ""),
        "author": post_data.get("author", ""),
        "post_url": url,
        "top_comment": top_comment,
    }


def extract_tracks(post: dict) -> list[dict]:
    """
    Extract artist/title pairs from selftext then top_comment.
    Returns the longer list (selftext usually wins for track-list posts).
    """
    from_self = _extract_from_text(post.get("selftext") or "")
    from_comment = _extract_from_text(post.get("top_comment") or "")
    return from_self if len(from_self) >= len(from_comment) else from_comment
