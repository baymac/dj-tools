"""Instagram client wrapper using instagrapi."""

from __future__ import annotations

from typing import Any, Callable

import httpx
from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired
from instagrapi.types import Comment, Media

from paths import IG_SESSION_FILE as SESSION_FILE

# Signature: (username: str, choice: int) -> str
# choice 1 = email, 0 = SMS — mirrors instagrapi's ChallengeChoice enum
ChallengeHandler = Callable[[str, int], str]
# Signature: () -> str
TwoFactorHandler = Callable[[], str]


def build_client(
    username: str,
    password: str,
    challenge_handler: ChallengeHandler | None = None,
    two_factor_handler: TwoFactorHandler | None = None,
) -> Client:
    """Authenticate and return an Instagram client, reusing a cached session when possible."""
    cl = Client()
    cl.delay_range = [1, 3]
    if challenge_handler:
        cl.challenge_code_handler = challenge_handler

    def _login() -> None:
        try:
            cl.login(username, password)
        except TwoFactorRequired:
            if two_factor_handler is None:
                raise
            code = two_factor_handler()
            cl.two_factor_login(code)

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            _login()
            cl.dump_settings(SESSION_FILE)
            return cl
        except Exception:
            SESSION_FILE.unlink(missing_ok=True)
            cl = Client()
            cl.delay_range = [1, 3]
            if challenge_handler:
                cl.challenge_code_handler = challenge_handler

    _login()
    cl.dump_settings(SESSION_FILE)
    return cl


def fetch_media(cl: Client, url: str) -> Media:
    """Return a Media object for the given Instagram post URL."""
    media_pk = cl.media_pk_from_url(url)
    return cl.media_info(media_pk)


def fetch_pinned_comment(cl: Client, media_id: str) -> Comment | None:
    """Return the first pinned comment on a post, or None."""
    comments = cl.media_comments(media_id, amount=50)
    for comment in comments:
        if getattr(comment, "type", 0) == 1:
            return comment
    return None


def fetch_top_comments(cl: Client, media_id: str, n: int = 5) -> list[Comment]:
    return cl.media_comments(media_id, amount=n)


def download_file(url: str, dest: str) -> None:
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)


def video_resources(media: Media) -> list[Any]:
    if media.media_type == 2:
        return [media]
    if media.media_type == 8:
        return [r for r in media.resources if getattr(r, "video_url", None)]
    return []
