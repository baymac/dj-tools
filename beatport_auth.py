#!/usr/bin/env python3
"""
Minimal Beatport auth CLI — captures and stores a Bearer token for use by
beatport_analyze.py.

Usage:
    uv run beatport_auth.py login              # headless browser login, prompts for creds
    uv run beatport_auth.py login -u USER -p PASS
    uv run beatport_auth.py status             # show token validity
    uv run beatport_auth.py clear              # delete stored token

Token is stored in .beatport_token alongside this script.
Auth code copied from ~/conductor/workspaces/beatport/chennai/beatport.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

TOKEN_FILE = Path(__file__).parent / ".beatport_token"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ── Token storage ──────────────────────────────────────────────────────────────

def load_token() -> str | None:
    """Return the stored Bearer token, or None if missing/expired."""
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        token = data.get("token")
        if not token:
            return None
        # Check JWT expiry
        exp = _jwt_exp(token)
        if exp and exp < time.time() + 60:  # 60s grace
            return None
        return token
    except Exception:
        return None


def save_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"token": token, "saved_at": time.time()}))
    TOKEN_FILE.chmod(0o600)


def _jwt_exp(token: str) -> float | None:
    """Decode the JWT exp claim without verifying signature."""
    try:
        payload = token.split()[-1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return float(decoded["exp"])
    except Exception:
        return None


def _jwt_info(token: str) -> dict:
    """Decode JWT payload fields (sub, exp, scope) for display."""
    try:
        payload = token.split()[-1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_user_scoped(token: str) -> bool:
    """Return True if token has user-level (non-anonymous) scope."""
    try:
        info = _jwt_info(token)
        return "user:anon" not in info.get("scope", "")
    except Exception:
        return True


# ── Playwright login (copied from chennai/beatport.py) ────────────────────────

async def _capture_token_async(username: str, password: str) -> str:
    from playwright.async_api import async_playwright

    captured: dict[str, str | None] = {"token": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()

        def grab(req) -> None:
            auth = req.headers.get("authorization", "")
            if "api.beatport.com" in req.url and auth.startswith("Bearer "):
                captured["token"] = auth

        page.on("request", grab)
        await page.goto("https://www.beatport.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        try:
            await page.locator("#onetrust-accept-btn-handler").click(timeout=2000)
        except Exception:
            pass
        await (
            page.get_by_role("link", name="Login").or_(
                page.get_by_role("button", name="Login")
            ).first.click()
        )
        await page.wait_for_url(lambda u: "account.beatport.com" in u, timeout=20_000)
        await page.fill("input[name='username']", username)
        await page.fill("input[name='password']", password)
        await page.click("button[type='submit']")
        await page.wait_for_url(
            lambda u: "beatport.com/" in u and "account.beatport.com" not in u,
            timeout=20_000,
        )
        await page.wait_for_timeout(2500)
        await page.goto("https://www.beatport.com/library/playlists",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await browser.close()

    if not captured["token"]:
        raise RuntimeError(
            "Beatport login failed — could not capture Bearer token.\n"
            "Check your username/password and try again."
        )
    return captured["token"]


def capture_token(username: str, password: str) -> str:
    """Headless browser login → user-scoped Bearer token. Retries once on anon token."""
    token = asyncio.run(_capture_token_async(username, password))
    if not _is_user_scoped(token):
        print("  Got anonymous token, retrying...", file=sys.stderr)
        token = asyncio.run(_capture_token_async(username, password))
    return token


# ── CLI commands ───────────────────────────────────────────────────────────────

def cmd_login(username: str | None, password: str | None) -> None:
    if not username:
        username = input("Beatport username: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("Beatport password: ")

    print("Logging in (headless browser)...")
    try:
        token = capture_token(username, password)
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)

    save_token(token)
    info = _jwt_info(token)
    exp = info.get("exp")
    exp_str = time.strftime("%H:%M:%S", time.localtime(exp)) if exp else "unknown"
    print(f"Logged in as {info.get('sub', '?')}  (token valid until {exp_str})")
    print(f"Saved to {TOKEN_FILE}")


def cmd_status() -> None:
    if not TOKEN_FILE.exists():
        print("No token stored. Run: uv run beatport_auth.py login")
        return

    try:
        data = json.loads(TOKEN_FILE.read_text())
        token = data.get("token", "")
    except Exception:
        print("Token file corrupt.")
        return

    info = _jwt_info(token)
    exp = info.get("exp")
    now = time.time()

    if exp:
        remaining = exp - now
        if remaining > 0:
            print(f"Token valid  (expires in {int(remaining//60)}m {int(remaining%60)}s)")
        else:
            print(f"Token EXPIRED  ({int(-remaining//60)}m {int(-remaining%60)}s ago)")
    else:
        print("Token: no expiry info")

    print(f"  User:  {info.get('sub', '?')}")
    print(f"  Scope: {info.get('scope', '?')}")

    # Try a quick API ping
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.beatport.com/v4/catalog/keys/",
            headers={"authorization": token, "accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"  API ping: OK ({r.status})")
    except Exception as e:
        print(f"  API ping: FAILED — {e}")


def cmd_clear() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("Token cleared.")
    else:
        print("No token stored.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Beatport auth — login and store a Bearer token",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run beatport_auth.py login                  # interactive prompt
  uv run beatport_auth.py login -u me -p secret  # non-interactive
  uv run beatport_auth.py status                 # check validity + API ping
  uv run beatport_auth.py clear                  # remove stored token
        """,
    )
    sub = parser.add_subparsers(dest="cmd")

    p_login = sub.add_parser("login", help="Headless browser login")
    p_login.add_argument("-u", "--username")
    p_login.add_argument("-p", "--password")

    sub.add_parser("status", help="Show token validity")
    sub.add_parser("clear",  help="Delete stored token")

    args = parser.parse_args()

    if args.cmd == "login":
        cmd_login(args.username, args.password)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "clear":
        cmd_clear()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
