"""Reddit post track list fetcher — no audio, pure text parsing."""

from __future__ import annotations

import re
from typing import Optional

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
    Fetch a Reddit post using Playwright (bypasses the JSON API 403 block).
    Uses old.reddit.com for simpler, server-rendered HTML.
    Returns: {title, selftext, subreddit, author, post_url, top_comment}
    """
    from playwright.sync_api import sync_playwright

    old_url = re.sub(r"https?://(www\.)?reddit\.com", "https://old.reddit.com", url, count=1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(old_url, wait_until="domcontentloaded", timeout=30_000)

            title = (page.text_content(".top-matter .title a.title") or "").strip()

            subreddit = ""
            sr = page.query_selector(".subreddit")
            if sr:
                subreddit = (sr.text_content() or "").strip().lstrip("r/")

            author = ""
            auth = page.query_selector(".tagline .author")
            if auth:
                author = (auth.text_content() or "").strip()

            selftext = ""
            body = page.query_selector(".expando .usertext-body .md")
            if body:
                selftext = (body.inner_text() or "").strip()

            # Best comment: prefer stickied/mod comment, then first substantial one
            top_comment = ""
            for el in page.query_selector_all(".commentarea .thing.comment")[:10]:
                classes = el.get_attribute("class") or ""
                body_el = el.query_selector(".usertext-body .md")
                if not body_el:
                    continue
                text = (body_el.inner_text() or "").strip()
                if "stickied" in classes or "moderator" in classes:
                    top_comment = text
                    break
                if not top_comment and len(text) > 80:
                    top_comment = text
        finally:
            browser.close()

    return {
        "title": title,
        "selftext": selftext,
        "subreddit": subreddit,
        "author": author,
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
