"""Import cookies from Brave's local store for a given domain.

Avoids the "log in to a separate Playwright profile" friction — read your
existing Brave session directly. Works while Brave is running because the
SQLite store is opened read-only/immutable.

Cookie values are AES-CBC encrypted with a key derived from a macOS Keychain
password (`Brave Safe Storage`). On first run, macOS may prompt to grant the
running process access — approve once and pick "Always Allow" to skip future
prompts.

Chrome shares the same encryption scheme; flip `_KEYCHAIN_SERVICE` /
`_PROFILE_DIR` to support it later if needed.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Optional


_PROFILE_DIR = Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser/Default"
_COOKIES_DB = _PROFILE_DIR / "Cookies"
_KEYCHAIN_SERVICE = "Brave Safe Storage"

# Chromium derives its cookie encryption key from PBKDF2(password, "saltysalt", 1003)
_KDF_SALT = b"saltysalt"
_KDF_ITERATIONS = 1003
_KDF_KEY_LEN = 16
_AES_IV = b" " * 16  # 16 bytes of 0x20


def _get_keychain_password() -> str:
    """Read the Brave Safe Storage password via macOS `security` command.

    The first call typically pops a Keychain dialog; clicking 'Always Allow'
    avoids future prompts. Raises RuntimeError on denial / missing entry.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", _KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("`security` command not found — this only works on macOS.")
    if result.returncode != 0:
        raise RuntimeError(
            f"Couldn't read '{_KEYCHAIN_SERVICE}' from macOS Keychain. "
            "If a Keychain prompt appeared, click Allow / Always Allow. "
            "If not, open Keychain Access.app, find 'Brave Safe Storage', "
            "and grant access manually."
        )
    return result.stdout.strip()


def _pkcs7_unpad(data: bytes) -> Optional[bytes]:
    """Remove PKCS#7 padding. Returns None if padding is invalid."""
    if not data:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16:
        return None
    if len(data) < pad:
        return None
    return data[:-pad]


def _decrypt_value(encrypted: bytes, key: bytes) -> Optional[str]:
    """Decrypt a Chromium cookie blob (`v10`/`v11` + AES-CBC). None on failure.

    Chromium 90+ on macOS prepends a 32-byte SHA-256 of the host_key to the
    plaintext before encryption (integrity check). On newer builds where this
    prefix was removed, we fall back to treating the full plaintext as the
    cookie value. Both paths are tried before giving up.
    """
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        from Crypto.Cipher import AES  # type: ignore[no-redef]

    if not encrypted:
        return None
    has_host_prefix = encrypted[:3] in (b"v10", b"v11")
    if has_host_prefix:
        encrypted = encrypted[3:]
    if len(encrypted) % 16 != 0 or len(encrypted) == 0:
        return None
    try:
        cipher = AES.new(key, AES.MODE_CBC, _AES_IV)
        plaintext = cipher.decrypt(encrypted)

        if has_host_prefix and len(plaintext) >= 32:
            # Try stripping the 32-byte sha256(host_key) prefix first.
            stripped = _pkcs7_unpad(plaintext[32:])
            if stripped is not None:
                return stripped.decode("utf-8", errors="replace")
            # Prefix not present in this Chromium build — try without stripping.
            fallback = _pkcs7_unpad(plaintext)
            if fallback is not None:
                return fallback.decode("utf-8", errors="replace")
            return None

        result = _pkcs7_unpad(plaintext)
        if result is None:
            return None
        return result.decode("utf-8", errors="replace")
    except Exception:
        return None


def _derive_key(password: str) -> bytes:
    try:
        from Cryptodome.Protocol.KDF import PBKDF2
    except ImportError:
        from Crypto.Protocol.KDF import PBKDF2  # type: ignore[no-redef]
    return PBKDF2(password, _KDF_SALT, dkLen=_KDF_KEY_LEN, count=_KDF_ITERATIONS)


def _chromium_to_unix_seconds(chromium_us: int) -> float:
    """Convert Chromium's microseconds-since-1601 timestamp to Unix seconds.

    Returns -1 (Playwright's "session cookie") for missing or zero values.
    """
    if not chromium_us or chromium_us <= 0:
        return -1
    return chromium_us / 1_000_000 - 11_644_473_600


def read_cookies_for_domain(domain_substring: str) -> list[dict]:
    """Return Playwright-shaped cookie dicts for hosts matching `domain_substring`.

    `domain_substring` matches via SQL `LIKE '%<sub>%'` so passing
    'soundcloud.com' matches `.soundcloud.com`, `m.soundcloud.com`, etc.
    Raises RuntimeError if Brave isn't installed, cookies can't be read,
    or the keychain password is unavailable.
    """
    if not _COOKIES_DB.exists():
        raise RuntimeError(
            f"Brave cookie store not found at {_COOKIES_DB}. Is Brave installed and "
            "have you logged into SoundCloud in it at least once?"
        )

    password = _get_keychain_password()
    key = _derive_key(password)

    # Read-only + immutable lets us open the DB even while Brave is running,
    # bypassing the SQLite write lock.
    uri = f"file:{_COOKIES_DB}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT name, encrypted_value, host_key, path, expires_utc, "
            "is_secure, is_httponly, samesite FROM cookies "
            "WHERE host_key LIKE ?",
            (f"%{domain_substring}%",),
        ).fetchall()
    finally:
        con.close()

    cookies: list[dict] = []
    for name, enc_value, host, path, expires_us, is_secure, is_httponly, samesite in rows:
        value = _decrypt_value(enc_value, key)
        if value is None:
            continue
        same_site_map = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "expires": _chromium_to_unix_seconds(expires_us),
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": same_site_map.get(samesite, "Lax"),
        })
    return cookies
