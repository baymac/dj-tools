#!/usr/bin/env python3
"""Recon a small set of lessons via live DOM queries.

Key insight: Circle (the platform) renders the video player dynamically.
`page.content()` misses things; we must query the live DOM via evaluate().

For each lesson:
  1. Navigates the headed browser
  2. Waits for content to settle (8s — SPA needs time)
  3. Saves a screenshot
  4. Saves a JSON report of *live DOM signals*

Output: ~/Music/dj-tools/course/_recon/index.md + per-lesson .png + .json
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from helpers.download_course import _close_context, _open_context  # noqa: E402
from paths import COURSE_DIR  # noqa: E402

RECON_DIR = COURSE_DIR / "_recon"

import json as _json
_lessons = _json.loads((Path.home()/'Music/dj-tools/course/lessons.json').read_text())
_idx_to_url = lambda i: _lessons[i-1]['url']

TARGETS = [
    # Lesson 6: "Decks focus" — sidebar shows EMPTY CIRCLE (unlocked, not completed yet).
    # This is the ONLY one in this state — gives us the pre-complete button view.
    ("06_decks_focus_uncompleted", "Lesson 2: Decks focus - Alex from The Reloud",
     _idx_to_url(6)),
]


# Queries the LIVE DOM (handles Shadow DOM where possible) and dumps everything
# we need to understand the page type.
SIGNAL_JS = r"""
() => {
    // Helper: pierce shadow DOM
    function* allElements(root = document) {
        const stack = [root];
        while (stack.length) {
            const node = stack.pop();
            if (!node || !node.querySelectorAll) continue;
            yield node;
            // iterate light DOM
            for (const el of node.querySelectorAll('*')) {
                yield el;
                if (el.shadowRoot) stack.push(el.shadowRoot);
            }
        }
    }

    const out = {
        url: location.href,
        title: document.title,
        body_text_len: (document.body.innerText || '').length,
        videos: [],
        sources: [],
        iframes: [],
        circle_cdn_urls: [],
        buttons: [],
        complete_buttons: [],
        nav_buttons: [],
        forms: [],
        radios: 0,
        download_links: [],
        attachments_button: null,
        sidebar_state: { completed: 0, locked: 0, available: 0, total: 0 },
        candidate_content: null,
        body_text_preview: '',
    };

    // 1. Live <video> + <source> elements
    for (const v of allElements()) {
        if (v.tagName === 'VIDEO') {
            out.videos.push({
                src: v.currentSrc || v.src || '',
                poster: v.poster || '',
                duration: v.duration || null,
                paused: v.paused,
                inner: v.innerHTML.slice(0, 200),
            });
        }
        if (v.tagName === 'SOURCE') {
            out.sources.push({
                src: v.src || v.getAttribute('src') || '',
                type: v.type || '',
            });
        }
        if (v.tagName === 'IFRAME') {
            out.iframes.push({
                src: (v.getAttribute('src') || '').slice(0, 200),
                classes: (v.className || '').slice(0, 80),
                title: (v.getAttribute('title') || '').slice(0, 80),
                width: v.width, height: v.height,
            });
        }
    }

    // 2. Find any circle.so CDN URL anywhere in the DOM (innerHTML scan)
    const fullHTML = document.documentElement.outerHTML;
    const cdnRegex = /https?:\/\/[^"'\s]*cdn-media\.circle\.so[^"'\s]*/g;
    const cdnSet = new Set();
    let m;
    while ((m = cdnRegex.exec(fullHTML)) !== null) cdnSet.add(m[0]);
    out.circle_cdn_urls = Array.from(cdnSet).slice(0, 8);

    // 3. All buttons with their text, identify "complete"-like
    for (const b of document.querySelectorAll('button')) {
        const txt = (b.textContent || '').trim();
        const aria = b.getAttribute('aria-label') || '';
        out.buttons.push({
            text: txt.slice(0, 60),
            aria: aria.slice(0, 60),
            disabled: b.disabled,
        });
        if (/complet|finish|done|next lesson|mark.+done/i.test(txt + ' ' + aria)) {
            out.complete_buttons.push({
                text: txt.slice(0, 80),
                aria: aria.slice(0, 80),
                classes: (b.className || '').slice(0, 100),
                disabled: b.disabled,
                outer: b.outerHTML.slice(0, 400),
            });
        }
        if (/next|previous|prev|→|←/i.test(txt + ' ' + aria) && txt.length < 30) {
            out.nav_buttons.push({ text: txt.slice(0, 40), aria: aria.slice(0, 40) });
        }
    }

    // 4. Forms / radios for quiz signal
    for (const f of document.querySelectorAll('form')) {
        out.forms.push({
            id: f.id || '',
            action: (f.action || '').slice(0, 120),
            inputs: f.querySelectorAll('input').length,
            radios: f.querySelectorAll('input[type=radio]').length,
            classes: (f.className || '').slice(0, 100),
        });
    }
    out.radios = document.querySelectorAll('input[type=radio]').length;

    // 5. Download links — file-extension or download attr
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        if (a.hasAttribute('download') || /\.(zip|pdf|mp3|wav|aiff?|flac|docx?|xlsx?|pptx?|csv|txt)(\?|$)/i.test(href)) {
            out.download_links.push({
                name: (a.textContent || '').trim().slice(0, 100),
                href: href.slice(0, 250),
            });
        }
    }

    // 6. Attachment toggle — folder icon button
    const attBtn = document.querySelector(
        '[data-lesson-attachments-button], [aria-label*="attachment" i], ' +
        '[aria-label*="files" i], button[class*="attachment"], ' +
        'button[class*="files-toggle"]'
    );
    if (attBtn) out.attachments_button = {
        tag: attBtn.tagName,
        aria: attBtn.getAttribute('aria-label') || '',
        classes: (attBtn.className || '').slice(0, 120),
    };

    // 7. Sidebar lesson state — count green checks, padlocks, etc.
    // Padlock + checkmark + circle indicators in the sidebar
    const sidebarItems = document.querySelectorAll('aside a, [class*="sidebar"] a, nav a');
    out.sidebar_state.total = sidebarItems.length;
    for (const a of sidebarItems) {
        const html = a.innerHTML;
        // crude — look for SVG icon shapes
        if (/lock/i.test(html) || /padlock/i.test(html)) out.sidebar_state.locked++;
        else if (/check/i.test(html) || /✓/.test(a.textContent)) out.sidebar_state.completed++;
        else out.sidebar_state.available++;
    }

    // 8. Candidate content — try to find the actual lesson body
    // Strategy: find an element with substantial innerText (> 200 chars)
    // that is NOT in the sidebar, AND not the whole document
    const allDivs = Array.from(document.querySelectorAll('main div, article div, [role="main"] div'));
    let best = null;
    for (const el of allDivs) {
        const txt = (el.innerText || '').trim();
        if (txt.length < 80 || txt.length > 50000) continue;
        // skip if any ancestor is the sidebar
        let p = el.parentElement;
        let inSidebar = false;
        while (p) {
            if (/sidebar|aside/i.test(p.tagName + ' ' + (p.className || ''))) {
                inSidebar = true;
                break;
            }
            p = p.parentElement;
        }
        if (inSidebar) continue;
        if (!best || txt.length > best.text_len) {
            best = {
                tag: el.tagName,
                classes: (el.className || '').slice(0, 200),
                id: el.id,
                text_len: txt.length,
                html_len: el.innerHTML.length,
                text_preview: txt.slice(0, 300),
            };
        }
    }
    out.candidate_content = best;

    // 9. Quick-look text preview
    out.body_text_preview = (document.body.innerText || '').slice(0, 600);

    return out;
}
"""


async def recon_one(ctx, slug: str, title: str, url: str) -> dict:
    page = await ctx.new_page()
    page.set_default_timeout(30000)
    print(f"\n--- {slug}: {title} ---")
    print(f"  {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for the SPA to fully hydrate — Circle renders video player late
        await page.wait_for_timeout(8000)

        png = RECON_DIR / f"{slug}.png"
        await page.screenshot(path=str(png), full_page=True)

        signals = await page.evaluate(SIGNAL_JS)
        signals["slug"] = slug
        signals["target_title"] = title
        signals["screenshot"] = png.name

        # Save JSON
        (RECON_DIR / f"{slug}.json").write_text(
            json.dumps(signals, indent=2), encoding="utf-8"
        )

        # Stdout summary
        print(f"  body_text={signals['body_text_len']}")
        print(f"  videos={len(signals['videos'])}  sources={len(signals['sources'])}  "
              f"iframes={len(signals['iframes'])}")
        print(f"  circle_cdn_urls={len(signals['circle_cdn_urls'])}")
        if signals['circle_cdn_urls']:
            for u in signals['circle_cdn_urls'][:2]:
                print(f"    cdn: {u[:140]}")
        print(f"  forms={len(signals['forms'])}  radios={signals['radios']}  "
              f"downloads={len(signals['download_links'])}")
        print(f"  complete_btns={len(signals['complete_buttons'])}  "
              f"sidebar(completed/locked/avail)={signals['sidebar_state']['completed']}/"
              f"{signals['sidebar_state']['locked']}/{signals['sidebar_state']['available']}")
        if signals['complete_buttons']:
            print(f"    complete: {signals['complete_buttons'][0]['text']!r}")
        if signals['candidate_content']:
            cc = signals['candidate_content']
            print(f"  content: <{cc['tag']}> len={cc['text_len']}  classes={cc['classes'][:80]}")
            print(f"    preview: {cc['text_preview'][:160]!r}")

        return signals
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main():
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Recon → {RECON_DIR}")
    print("Opening headed browser…")
    p_api, ctx = await _open_context(headless=False)

    results = []
    try:
        for slug, title, url in TARGETS:
            try:
                sig = await recon_one(ctx, slug, title, url)
                results.append(sig)
            except Exception as exc:
                print(f"  RECON ERROR ({type(exc).__name__}): {exc}")
                results.append({
                    "slug": slug, "target_title": title, "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    finally:
        await _close_context(p_api, ctx)

    (RECON_DIR / "signals.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # Markdown summary
    md = ["# Course recon (live DOM)\n"]
    for r in results:
        md.append(f"## {r['slug']}: {r.get('target_title','')}")
        md.append(f"- url: {r.get('url','')}")
        if "error" in r:
            md.append(f"- **ERROR**: {r['error']}\n")
            continue
        md.append(f"- screenshot: `{r['screenshot']}` · body_text={r['body_text_len']}")
        md.append(f"- videos: {r['videos']}")
        md.append(f"- sources: {r['sources']}")
        md.append(f"- circle_cdn_urls ({len(r['circle_cdn_urls'])}):")
        for u in r['circle_cdn_urls'][:5]:
            md.append(f"    - `{u[:200]}`")
        if r['iframes']:
            md.append(f"- iframes: {[(i['src'][:60] or i['classes'][:30]) for i in r['iframes'][:5]]}")
        if r['forms'] or r['radios']:
            md.append(f"- forms: {r['forms']}  radios: {r['radios']}")
        if r['download_links']:
            md.append(f"- downloads: {[d['name'] for d in r['download_links'][:5]]}")
        if r['complete_buttons']:
            md.append(f"- complete_buttons:")
            for b in r['complete_buttons'][:3]:
                md.append(f"    - text={b['text']!r} disabled={b['disabled']} classes={b['classes'][:60]}")
        if r['attachments_button']:
            md.append(f"- attachments_button: {r['attachments_button']}")
        ss = r['sidebar_state']
        md.append(f"- sidebar: total={ss['total']} completed={ss['completed']} locked={ss['locked']} avail={ss['available']}")
        if r['candidate_content']:
            cc = r['candidate_content']
            md.append(f"- candidate_content: <{cc['tag']}> text_len={cc['text_len']} html_len={cc['html_len']}")
            md.append(f"    classes: `{cc['classes'][:160]}`")
            md.append(f"    preview: {cc['text_preview'][:240]!r}")
        md.append(f"- body_text_preview: {r['body_text_preview'][:300]!r}")
        md.append("")
    (RECON_DIR / "index.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nWrote {RECON_DIR / 'index.md'}")


if __name__ == "__main__":
    asyncio.run(main())
