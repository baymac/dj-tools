#!/usr/bin/env python3
"""Open the persistent course-browser profile, close every zombie tab, exit clean.

Use after a crashed or interrupted run leaves blank tabs accumulated in the
session-restore state. Subsequent runs of the scraper will then start fresh.

    uv run helpers/course_cleanup_tabs.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from helpers.download_course import _close_context, _open_context  # noqa: E402


async def main():
    print("Opening profile (headless) to sweep zombie tabs…")
    p, ctx = await _open_context(headless=True)
    print(f"  found {len(ctx.pages)} open page(s) — closing all")
    # _open_context already does an initial sweep, but keep this for any pages
    # that opened between then and now (e.g. background scripts).
    for page in list(ctx.pages):
        try:
            await page.close()
        except Exception:
            pass
    await _close_context(p, ctx)
    print("Done. Profile session is now clean.")


if __name__ == "__main__":
    asyncio.run(main())
