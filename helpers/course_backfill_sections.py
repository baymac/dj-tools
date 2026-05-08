#!/usr/bin/env python3
"""Re-discover chapter sections and patch the existing lessons.json in place.

Run after fixing _scrape_lesson_list — this rebuilds sectionTitle / sectionIndex
for every lesson without re-scraping content.

    uv run helpers/course_backfill_sections.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from helpers.download_course import _close_context, _open_context, _scrape_lesson_list  # noqa: E402
from paths import COURSE_DIR  # noqa: E402

MANIFEST = COURSE_DIR / "lessons.json"
COURSE_URL = "https://campus.petetong-djacademy.com/c/dj-full-course/sections/660774/lessons/917531"


async def main():
    if not MANIFEST.exists():
        print(f"No manifest at {MANIFEST}")
        return

    print("Opening browser…")
    p, ctx = await _open_context(headless=True)
    try:
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        print("Re-discovering chapter sections…")
        fresh = await _scrape_lesson_list(page, COURSE_URL)
        await page.close()
    finally:
        await _close_context(p, ctx)

    by_id = {l.id: l for l in fresh}
    print(f"  discovered {len(fresh)} lessons across {len({l.section_title for l in fresh})} chapters")

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    patched = 0
    for entry in data:
        f = by_id.get(entry["id"])
        if not f:
            continue
        if entry.get("sectionTitle") != f.section_title or entry.get("sectionIndex") != f.section_index:
            entry["sectionTitle"] = f.section_title
            entry["sectionIndex"] = f.section_index
            patched += 1
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  patched {patched} lessons in {MANIFEST}")

    # Show updated section breakdown
    from collections import Counter
    counts = Counter(e["sectionTitle"] for e in data)
    print("\nSection breakdown after backfill:")
    for s, n in counts.most_common():
        print(f"  ({n}) {s}")


if __name__ == "__main__":
    asyncio.run(main())
