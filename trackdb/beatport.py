"""Beatport API helpers for the trackdb pipeline."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Optional

_LOCAL_TOKEN_FILE = Path(__file__).parent.parent / "local-analyse" / ".beatport_token"
_CHENNAI_DB = Path.home() / "conductor/workspaces/beatport/chennai/state/sync.db"
API_ROOT = "https://api.beatport.com/v4"


def _jwt_exp(token: str) -> Optional[float]:
    try:
        payload = token.split()[-1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def load_token() -> Optional[str]:
    """Return a valid Bearer token from local store or chennai DB, or None."""
    if _LOCAL_TOKEN_FILE.exists():
        try:
            data = json.loads(_LOCAL_TOKEN_FILE.read_text())
            token = data.get("token", "")
            exp = _jwt_exp(token)
            if token and (exp is None or exp > time.time() + 60):
                return token
        except Exception:
            pass

    if _CHENNAI_DB.exists():
        try:
            con = sqlite3.connect(str(_CHENNAI_DB))
            row = con.execute(
                "SELECT token FROM auth_cache WHERE service='beatport'"
            ).fetchone()
            con.close()
            if row:
                token = row[0]
                exp = _jwt_exp(token)
                if exp is None or exp > time.time() + 60:
                    return token
        except Exception:
            pass

    return None


def api_get(path: str, token: str) -> dict:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    req = urllib.request.Request(
        url,
        headers={"authorization": token, "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_release_date(beatport_id: str, token: str) -> Optional[str]:
    """Return release date (YYYY-MM-DD) for a Beatport track ID, or None."""
    try:
        data = api_get(f"/catalog/tracks/{beatport_id}/", token)
        # publish_date is when it went live on Beatport; release.date is label date
        date = data.get("publish_date") or data.get("new_release_date")
        if not date:
            release = data.get("release", {})
            date = release.get("date") if isinstance(release, dict) else None
        return date or None
    except Exception:
        return None
