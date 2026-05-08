#!/usr/bin/env python3
"""Download a Pete Tong DJ Academy / Circle course for offline viewing.

The course is gated: each lesson must be marked complete before the next unlocks.
Videos use TWO platforms — Circle's native HLS player and Dyntube iframes.

Usage:
    uv run helpers/download_course.py login    <course_url>
    uv run helpers/download_course.py download <course_url> [--limit N] [--dry-run] [--lesson-ids ID1,ID2,...]

Stages:
  1. Login: opens headed browser, you sign in, session saved to persistent profile
  2. Download:
     a. Discover all lessons (sidebar walk → ordered list)
     b. For each lesson IN ORDER:
        - Navigate, wait for content
        - Set up network sniffer for video URLs
        - Classify page type
        - Extract data per type
        - Click "Complete lesson" if needed (unlocks next)
        - Persist manifest after each lesson

Output: ~/Music/dj-tools/course/{lessons.json, videos/, files/, images/, quizzes/}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import Response, async_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import COURSE_DIR, STATE_DIR, open_log  # noqa: E402
from caffeinate import caffeinate  # noqa: E402

class _Tee:
    """Write to multiple streams simultaneously (used to mirror stdout to log)."""
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
    def flush(self):
        for s in self._streams:
            s.flush()


VIDEOS_DIR = COURSE_DIR / "videos"
IMAGES_DIR = COURSE_DIR / "images"
FILES_DIR = COURSE_DIR / "files"
QUIZZES_DIR = COURSE_DIR / "quizzes"
THUMBS_DIR = COURSE_DIR / "thumbs"
SUBS_DIR = COURSE_DIR / "subtitles"
KEYS_DIR = COURSE_DIR / "_keys"        # local cache of captured AES keys
HLS_DIR = COURSE_DIR / "_hls"          # local cache of rewritten m3u8 manifests
MANIFEST_FILE = COURSE_DIR / "lessons.json"
FAILED_FILE = COURSE_DIR / "failed.json"

COURSE_PROFILE_DIR = STATE_DIR / "course-browser-profile"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

# The lesson body container — discovered via recon. Tailwind's `max-w-[800px]`
# class would need backslash-escaping in CSS. Bracket-free attribute selector
# works for both Playwright and JS, no escaping headaches.
CONTENT_SELECTOR = '[class*="mx-auto"][class*="max-w-"][class*="space-y-8"]'


class LessonType(str, Enum):
    LOCKED = "locked"
    VIDEO_CIRCLE = "video_circle"
    VIDEO_DYNTUBE = "video_dyntube"
    QUIZ = "quiz"
    EXERCISE = "exercise"
    GUIDE = "guide"
    CONTENT = "content"
    UNKNOWN = "unknown"


@dataclass
class Attachment:
    name: str
    file: str       # relative path under course dir
    size: str = ""


@dataclass
class Subtitle:
    label: str       # e.g. "Captions 1" or "English"
    file: str        # relative path under course dir, e.g. "subtitles/<id>/0.vtt"
    lang: str = "en" # BCP-47 hint for <track srclang>
    default: bool = False


@dataclass
class Lesson:
    id: str
    section_title: str
    section_index: int
    lesson_index: int
    title: str
    url: str
    # Populated by walker
    type: str = LessonType.UNKNOWN.value
    extracted: bool = False
    completed: bool = False         # did we click "Complete lesson"
    video_url: Optional[str] = None
    video_file: Optional[str] = None
    thumb_file: Optional[str] = None
    content_html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    subtitles: list[Subtitle] = field(default_factory=list)
    quiz_file: Optional[str] = None  # quizzes/<id>.json
    error: Optional[str] = None

    def to_manifest(self) -> dict:
        return {
            "id": self.id,
            "sectionTitle": self.section_title,
            "sectionIndex": self.section_index,
            "lessonIndex": self.lesson_index,
            "title": self.title,
            "url": self.url,
            "type": self.type,
            "extracted": self.extracted,
            "completed": self.completed,
            "videoFile": self.video_file,
            "videoUrl": self.video_url,
            "thumbFile": self.thumb_file,
            "contentHtml": self.content_html,
            "attachments": [asdict(a) for a in self.attachments],
            "subtitles": [asdict(s) for s in self.subtitles],
            "quizFile": self.quiz_file,
            "error": self.error,
        }


# ---------- helpers ----------

def _find_real_browser() -> Optional[str]:
    for path in _BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:60] or "lesson"


def _cookies_to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        if domain and not domain.startswith("."):
            domain = f".{domain}"
        expiry = max(int(c.get("expires", 0) or 0), 0)
        lines.append("\t".join([
            domain, "TRUE", c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expiry), c.get("name", ""), c.get("value", ""),
        ]))
    return "\n".join(lines)


# ---------- browser context ----------

async def _open_context(headless: bool = True):
    p = await async_playwright().start()
    exe = _find_real_browser()
    COURSE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not headless:
        args += ["--window-size=1440,900"]
    ctx = await p.chromium.launch_persistent_context(
        str(COURSE_PROFILE_DIR),
        headless=headless,
        args=args,
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        **({"executable_path": exe} if exe else {}),
    )

    # Sanity sweep: close any zombie pages restored from a prior crashed run.
    # Chromium's persistent profile reopens pages that were live at last close.
    for page in list(ctx.pages):
        try:
            await page.close()
        except Exception:
            pass
    return p, ctx


async def _close_context(p_api, ctx) -> None:
    """Close every page, then the context, then the playwright API.

    Closing pages BEFORE ctx.close() prevents Chromium's persistent profile
    from saving session-restore state. Otherwise every page still open at close
    reappears as a blank tab on next launch and they accumulate.
    """
    try:
        for page in list(ctx.pages):
            try:
                await page.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        await ctx.close()
    except Exception:
        pass
    try:
        await p_api.stop()
    except Exception:
        pass


# ---------- DOM signal extraction (live DOM, pierces some shadow DOM) ----------

# This script runs inside the page and returns everything the classifier needs.
SIGNAL_JS = r"""
() => {
    const out = {
        title: '',
        body_text_len: 0,
        videos: [],
        sources: [],
        iframes: [],
        forms: 0,
        radios: 0,
        download_links: [],
        complete_button: null,   // { text, disabled }
        content_html: '',        // the lesson body div's innerHTML
        content_text: '',        // ... innerText, for classification
        is_locked: false,
    };

    // Lesson body container — bracket-free selector dodges CSS escape issues
    const body = document.querySelector('[class*="mx-auto"][class*="max-w-"][class*="space-y-8"]');
    if (body) {
        out.content_text = (body.innerText || '').trim();
        out.body_text_len = out.content_text.length;

        // Prefer the TipTap editor content — it's just the prose,
        // not the lesson header chrome or the embedded video player.
        const tiptap = body.querySelector('[data-testid="tip-tap-editor-content"]');
        const sourceEl = tiptap || body;

        // Clone so we can mutate without affecting the live page
        const clone = sourceEl.cloneNode(true);
        // Strip ONLY embedded video players (node-embed), not images (node-image).
        // We render the video ourselves; image embeds we want to keep so the
        // <img> rendered by the TipTap react-renderer stays in the markup.
        clone.querySelectorAll(
            'iframe, video, source, ' +
            'media-controller, hls-video, media-theme, template, ' +
            '[class*="react-renderer"][class*="node-embed"]'
        ).forEach(el => el.remove());

        out.content_html = clone.innerHTML;
    }
    out.is_locked = /lesson locked/i.test(out.content_text);

    // <video> + <source>
    for (const v of document.querySelectorAll('video')) {
        const src = v.currentSrc || v.src || '';
        out.videos.push({
            src, poster: v.poster || '',
            duration: v.duration || null,
        });
        for (const s of v.querySelectorAll('source')) {
            out.sources.push({
                src: s.src || s.getAttribute('src') || '',
                type: s.type || '',
            });
        }
    }
    // Top-level <source> too (some players)
    for (const s of document.querySelectorAll('source')) {
        out.sources.push({
            src: s.src || s.getAttribute('src') || '',
            type: s.type || '',
        });
    }

    // iframes (skip Stripe analytics noise)
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.getAttribute('src') || '';
        if (src.includes('js.stripe.com')) continue;
        out.iframes.push({
            src, classes: (f.className || '').slice(0, 80),
            width: f.width, height: f.height,
        });
    }

    // Forms (quiz signal)
    out.forms = document.querySelectorAll('form').length;
    out.radios = document.querySelectorAll('input[type=radio]').length;
    // Circle's quizzes use a hidden quizzes_question_id input rather than radios.
    out.is_quiz = !!document.querySelector('input[name*="quizzes_question_id"]');

    // Download links
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        if (a.hasAttribute('download') ||
            /\.(zip|pdf|mp3|wav|aiff?|flac|docx?|xlsx?|pptx?|csv|txt)(\?|$)/i.test(href)) {
            out.download_links.push({
                name: (a.textContent || '').trim().slice(0, 100),
                href: href.slice(0, 400),
            });
        }
    }

    // "Complete lesson" / "Completed" submit button — discovered by recon
    for (const b of document.querySelectorAll('button[type="submit"]')) {
        const txt = (b.textContent || '').trim();
        if (/complete/i.test(txt)) {
            out.complete_button = {
                text: txt,
                disabled: b.disabled,
                classes: (b.className || '').slice(0, 200),
            };
            break;
        }
    }

    // Page title — must come from inside the lesson body, not the page header.
    // The sidebar's "DJ Course" h1 would otherwise win.
    if (body) {
        const h = body.querySelector('h2, h3, h1');
        if (h) out.title = (h.textContent || '').trim();
    }

    return out;
}
"""


# ---------- discovery ----------

async def _scrape_lesson_list(page, course_url: str) -> list[Lesson]:
    """Walk the sidebar to discover all lessons, in display order.

    Returns Lesson stubs with id/section/title/url populated.
    Each lesson is tagged with its chapter (e.g. "CHAPTER 2 : WHERE TO SOURCE
    YOUR TRACKS") via a document-order walk of the lesson sidebar — the
    most-recently-seen chapter button is the lesson's section.
    """
    await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    parsed = urlparse(course_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    raw = await page.evaluate(r"""
        () => {
            const linkPattern = /\/sections\/\d+\/lessons\/\d+/;
            // Find the lesson list container — smallest ancestor of all lesson <a>s.
            const allLinks = document.querySelectorAll(
                'a[href*="/sections/"][href*="/lessons/"]'
            );
            if (!allLinks.length) return [];
            let container = allLinks[0].parentElement;
            while (container) {
                const inside = container.querySelectorAll(
                    'a[href*="/sections/"][href*="/lessons/"]'
                ).length;
                if (inside >= allLinks.length) break;
                container = container.parentElement;
            }
            if (!container) container = document.body;

            // Chapter button class signature observed in recon.
            const isChapterButton = (el) =>
                el.tagName === 'BUTTON' &&
                el.className.includes('text-dark') &&
                el.className.includes('w-full') &&
                el.className.includes('text-left');

            // Walk document order, tracking current chapter
            let current = '';
            const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT, null);
            const out = [];
            let node;
            while ((node = walker.nextNode())) {
                if (isChapterButton(node)) {
                    const t = (node.textContent || '').trim();
                    if (t.length > 3 && t.length < 200) current = t;
                } else if (node.tagName === 'A') {
                    const href = node.getAttribute('href') || '';
                    if (linkPattern.test(href)) {
                        out.push({
                            href,
                            text: (node.innerText || '').trim(),
                            section: current,
                        });
                    }
                }
            }
            return out;
        }
    """)

    seen: set[str] = set()
    lessons: list[Lesson] = []
    sections_order: list[str] = []
    section_map: dict[str, int] = {}
    current_section = "Course"

    for anchor in raw:
        href = anchor.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)
        full_url = href if href.startswith("http") else urljoin(base, href)
        m = re.search(r"/lessons/(\d+)", href)
        lesson_id = m.group(1) if m else re.sub(r"[^\w]", "_", href)[-16:]

        sec = anchor.get("section", "").strip() or current_section
        if sec not in section_map:
            section_map[sec] = len(sections_order)
            sections_order.append(sec)
        sec_idx = section_map[sec]
        current_section = sec

        title = anchor.get("text", "").strip() or f"Lesson {len(lessons) + 1}"
        lessons.append(Lesson(
            id=lesson_id,
            section_title=sec,
            section_index=sec_idx,
            lesson_index=len(lessons),
            title=title,
            url=full_url,
        ))
    return lessons


# ---------- classification ----------

def classify(signals: dict, lesson_title: str, network_urls: Optional[list[str]] = None) -> LessonType:
    """Decide page type from live DOM signals + network-captured URLs."""
    if signals.get("is_locked"):
        return LessonType.LOCKED

    network_urls = network_urls or []

    # Circle native HLS player — DOM source OR sniffed network URL
    for s in signals.get("sources", []):
        src = s.get("src") or ""
        if "cdn-media.circle.so" in src and \
           (s.get("type") == "application/x-mpegURL" or ".m3u8" in src):
            return LessonType.VIDEO_CIRCLE
    for url in network_urls:
        if "cdn-media.circle.so" in url and ".m3u8" in url:
            return LessonType.VIDEO_CIRCLE

    # Dyntube iframe (also from network if iframe didn't render)
    for f in signals.get("iframes", []):
        if "dyntube.com" in (f.get("src") or ""):
            return LessonType.VIDEO_DYNTUBE
        if "wistia" in (f.get("src") or "") or "vimeo" in (f.get("src") or ""):
            return LessonType.VIDEO_DYNTUBE
    for url in network_urls:
        if "dyntube.com" in url:
            return LessonType.VIDEO_DYNTUBE

    # Quiz: Circle's quizzes_question_id is the smoking gun (radios are rare here)
    if signals.get("is_quiz") or (
        signals.get("radios", 0) > 0 and signals.get("forms", 0) > 0
    ):
        return LessonType.QUIZ

    title_lower = (lesson_title or "").lower()
    # Match "Exercise:", "Exercise 1:", "Exercise 12:" etc.
    if re.match(r"^exercise(?:\s+\d+)?\s*:", title_lower) or signals.get("download_links"):
        return LessonType.EXERCISE
    if re.match(r"^guide(?:\s+\d+)?\s*:", title_lower):
        return LessonType.GUIDE

    if signals.get("body_text_len", 0) > 50:
        return LessonType.CONTENT

    return LessonType.UNKNOWN


# ---------- extraction ----------

async def _extract_lesson(page, lesson: Lesson, signals: dict, video_urls: list[str]) -> None:
    """Populate `lesson` fields based on its classified type."""
    lesson.content_html = signals.get("content_html") or ""

    if lesson.type == LessonType.VIDEO_CIRCLE.value:
        # Find the m3u8 source — DOM first, then network sniffer
        for s in signals.get("sources", []):
            src = s.get("src") or ""
            if "cdn-media.circle.so" in src and ".m3u8" in src:
                lesson.video_url = src
                break
        if not lesson.video_url:
            for url in video_urls:
                if "cdn-media.circle.so" in url and ".m3u8" in url:
                    lesson.video_url = url
                    break
        # Capture poster as thumbnail
        for v in signals.get("videos", []):
            if v.get("poster"):
                await _download_thumbnail(lesson, v["poster"])
                break

    elif lesson.type == LessonType.VIDEO_DYNTUBE.value:
        # Prefer the master HLS playlist (api.dyntube.com/.../hls-master?token=...)
        # since it lets yt-dlp pick the best quality and handle AES decryption.
        for url in video_urls:
            if "hls-master" in url and "dyntube.com" in url:
                lesson.video_url = url
                break
        # Fall back to a quality-specific m3u8
        if not lesson.video_url:
            for url in video_urls:
                if "dyntube" in url and ".m3u8" in url:
                    lesson.video_url = url
                    break
        # Fall back to mp4 if any
        if not lesson.video_url:
            for url in video_urls:
                if "dyntube" in url and ".mp4" in url:
                    lesson.video_url = url
                    break
        # Last resort: the iframe src — won't download but at least manifests something
        if not lesson.video_url:
            for f in signals.get("iframes", []):
                if "dyntube.com" in (f.get("src") or ""):
                    lesson.video_url = f["src"]
                    break

    elif lesson.type == LessonType.QUIZ.value:
        # Brute-force discover correct answers, save full quiz JSON
        await _extract_quiz(page, lesson)
        # After solving the quiz Circle may show a "Complete lesson" button that
        # wasn't present when we first read signals. Re-read now so the
        # completion click below sees the updated state.
        try:
            await page.wait_for_timeout(2000)
            signals = await page.evaluate(SIGNAL_JS)
        except Exception:
            pass

    # Attachments — for any type that might have files
    if signals.get("download_links"):
        for dl in signals["download_links"]:
            lesson.attachments.append(Attachment(
                name=dl.get("name", ""),
                file="",  # filled by downloader
                size="",
            ))
            lesson.attachments[-1].file = dl["href"]  # tmp store URL in `file` for downloader

    lesson.extracted = True


_QUIZ_STRUCTURE_JS = r"""
() => {
    // Walk all responses[i] question groups.
    // Question id lives on a hidden input via `name`; option ids live on
    // checkboxes via `id` (Circle leaves `name` empty for checkboxes — they
    // submit only the ones the user picked, the names aren't relevant).
    const groups = new Map();

    // Question IDs (hidden inputs with name="responses[i][quizzes_question_id]")
    for (const inp of document.querySelectorAll(
        'input[name^="responses["][name*="quizzes_question_id"]'
    )) {
        const m = inp.name.match(/responses\[(\d+)\]/);
        if (!m) continue;
        const idx = parseInt(m[1]);
        if (!groups.has(idx)) groups.set(idx, { idx, questionId: '', options: [] });
        groups.get(idx).questionId = inp.value;
    }

    // Option checkboxes (id="responses[i][selected_options][j][quizzes_option_id]")
    for (const inp of document.querySelectorAll(
        'input[type="checkbox"][id^="responses["]'
    )) {
        const m = inp.id.match(
            /responses\[(\d+)\]\[selected_options\]\[(\d+)\]\[quizzes_option_id\]/
        );
        if (!m) continue;
        const qIdx = parseInt(m[1]);
        const oIdx = parseInt(m[2]);
        if (!groups.has(qIdx)) groups.set(qIdx, { idx: qIdx, questionId: '', options: [] });
        // The visible text comes from a label[data-testid="item-label"][for=...]
        // or from the input's aria-label.
        const lbl = document.querySelector(
            `label[data-testid="item-label"][for="${inp.id}"]`
        ) || document.querySelector(`label[for="${inp.id}"]`);
        const text = lbl
            ? (lbl.textContent || '').trim()
            : (inp.getAttribute('aria-label') || inp.getAttribute('arialabel') || '').trim();
        groups.get(qIdx).options.push({
            idx: oIdx,
            id: inp.value || inp.id,
            inputId: inp.id,
            text,
        });
    }

    // Sort options within each group, then resolve question text + image
    const result = [];
    for (const g of [...groups.values()].sort((a, b) => a.idx - b.idx)) {
        g.options.sort((a, b) => a.idx - b.idx);
        const idAnchor = document.querySelector(
            `input[name="responses[${g.idx}][quizzes_question_id]"]`
        );
        if (idAnchor) {
            let scope = idAnchor.parentElement;
            for (let i = 0; i < 12 && scope; i++) {
                const candidates = scope.querySelectorAll(
                    'span.text-md.font-semibold, h2, h3, h4, label.font-semibold'
                );
                for (const c of candidates) {
                    const t = (c.textContent || '').trim();
                    if (t.length > 5 && t.length < 500 && /\?$/.test(t)) {
                        g.text = t;
                        break;
                    }
                }
                if (g.text) break;
                scope = scope.parentElement;
            }
            scope = idAnchor.parentElement;
            for (let i = 0; i < 8 && scope; i++) {
                const img = scope.querySelector('img[alt*="Question" i]');
                if (img) {
                    g.imageUrl = img.src;
                    break;
                }
                scope = scope.parentElement;
            }
        }
        result.push(g);
    }
    return result;
}
"""


async def _quiz_state(page) -> str:
    """Read current quiz state: 'fresh' / 'passed' / 'failed' / 'unknown'."""
    return await page.evaluate(r"""
        () => {
            const body = document.querySelector('[class*="mx-auto"][class*="max-w-"][class*="space-y-8"]');
            const text = body ? (body.innerText || '').trim().toLowerCase() : '';
            if (text.includes('you passed')) return 'passed';
            if (text.includes('try again') || text.includes('correct answers')) {
                const m = text.match(/correct answers\s*\n?\s*(\d+)\s*\/\s*(\d+)/);
                if (m && parseInt(m[1]) < parseInt(m[2])) return 'failed';
                return 'passed';
            }
            return 'fresh';
        }
    """)


async def _quiz_per_question_results(page) -> list[bool]:
    """After submission, read whether each question got 'Correct' badge."""
    return await page.evaluate("""
        () => {
            // Each question's outcome is a sibling div with text-v2-success ('Correct')
            // or text-v2-danger ('Wrong'). Walk per question scope.
            const results = [];
            for (const inp of document.querySelectorAll('input[name*="quizzes_question_id"]')) {
                let scope = inp.parentElement;
                let outcome = null;
                for (let i = 0; i < 12 && scope; i++) {
                    if (scope.querySelector('.text-v2-success, [class*="v2-success"]')) {
                        outcome = true; break;
                    }
                    if (scope.querySelector('.text-v2-danger, [class*="v2-danger"]')) {
                        outcome = false; break;
                    }
                    scope = scope.parentElement;
                }
                results.push(outcome === true);
            }
            return results;
        }
    """)


async def _retake_quiz(page) -> bool:
    """Click 'Retake quiz' if present. Returns True if it was clicked."""
    btn = page.locator('button:has-text("Retake")').first
    if await btn.count():
        try:
            await btn.click()
            await page.wait_for_timeout(3000)
            return True
        except Exception:
            return False
    return False


async def _select_quiz_option(page, q_idx: int, o_idx: int) -> bool:
    """Tick the checkbox for question q_idx, option o_idx. Returns True on success."""
    sel = f'label[for="responses[{q_idx}][selected_options][{o_idx}][quizzes_option_id]"]'
    try:
        await page.click(sel, timeout=5000)
        return True
    except Exception:
        return False


async def _extract_quiz(page, lesson: Lesson) -> None:
    """Brute-force discover correct answers for a Circle quiz, save to quizzes/<id>.json."""
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)

    # Already extracted — skip brute-force. The answers are on disk and the
    # lesson may already be complete on Circle's backend from the previous run.
    # Re-reading signals here lets the caller's completion-click logic detect
    # the current state (disabled button = auto-completed, "Completed" = done).
    quiz_out = QUIZZES_DIR / f"{lesson.id}.json"
    if quiz_out.exists():
        lesson.quiz_file = f"quizzes/{lesson.id}.json"
        print(f"    quiz: already extracted — skipping brute-force")
        return

    # Reset to a fresh quiz state if we previously passed/failed
    state = await _quiz_state(page)
    if state in ("passed", "failed"):
        await _retake_quiz(page)

    structure = await page.evaluate(_QUIZ_STRUCTURE_JS)
    if not structure:
        print(f"    quiz: no questions found — saving empty record")
        out = QUIZZES_DIR / f"{lesson.id}.json"
        out.write_text(json.dumps({
            "id": lesson.id, "title": lesson.title, "url": lesson.url,
            "questions": [], "html": lesson.content_html,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        lesson.quiz_file = f"quizzes/{lesson.id}.json"
        return

    print(f"    quiz: {len(structure)} question(s), brute-forcing correct answers…")

    # Single-question quiz: simple linear scan over options
    if len(structure) == 1:
        q = structure[0]
        correct_idx = None
        for o_idx in range(len(q["options"])):
            await _retake_quiz(page)
            if not await _select_quiz_option(page, 0, o_idx):
                continue
            try:
                await page.click(
                    'button[type="submit"]:has-text("Submit")', timeout=8000,
                )
            except Exception:
                continue
            await page.wait_for_timeout(3000)
            results = await _quiz_per_question_results(page)
            if results and results[0]:
                correct_idx = o_idx
                print(f"    quiz: option [{o_idx}] '{q['options'][o_idx]['text']}' is correct")
                break
        for o in q["options"]:
            o["correct"] = (o["idx"] == correct_idx)
    else:
        # Multi-question: fill every question with option [0], submit, read per-Q results.
        # For each wrong question, scan its options 1..N keeping known-correct ones.
        # Simpler heuristic V1: per-question linear scan, fill others with index 0.
        # Total submissions ≤ sum(len(options)) — bounded.
        for q in structure:
            for o in q["options"]:
                o["correct"] = False
        for q_idx in range(len(structure)):
            for o_idx in range(len(structure[q_idx]["options"])):
                await _retake_quiz(page)
                # Fill all questions
                for fill_q in range(len(structure)):
                    fill_o = o_idx if fill_q == q_idx else 0
                    await _select_quiz_option(page, fill_q, fill_o)
                try:
                    await page.click(
                        'button[type="submit"]:has-text("Submit")', timeout=8000,
                    )
                except Exception:
                    continue
                await page.wait_for_timeout(3000)
                results = await _quiz_per_question_results(page)
                if len(results) > q_idx and results[q_idx]:
                    structure[q_idx]["options"][o_idx]["correct"] = True
                    print(f"    quiz: q[{q_idx}] option [{o_idx}] is correct")
                    break

    # Download any quiz images locally
    for q in structure:
        if q.get("imageUrl"):
            await _download_quiz_image(lesson, q)

    # Save quiz file
    out = QUIZZES_DIR / f"{lesson.id}.json"
    out.write_text(json.dumps({
        "id": lesson.id,
        "title": lesson.title,
        "url": lesson.url,
        "questions": [
            {
                "id": q.get("questionId", ""),
                "text": q.get("text", ""),
                "image": q.get("imageLocal", ""),
                "options": [
                    {"id": o["id"], "text": o["text"], "correct": o.get("correct", False)}
                    for o in q["options"]
                ],
            }
            for q in structure
        ],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    lesson.quiz_file = f"quizzes/{lesson.id}.json"


async def _download_quiz_image(lesson: Lesson, q: dict) -> None:
    """Download a quiz question's image and stash a local path in q['imageLocal']."""
    url = q.get("imageUrl") or ""
    if not url:
        return
    qid = q.get("questionId", "img")
    ext_m = re.search(r'\.(jpg|jpeg|png|gif|webp|svg)', url, re.IGNORECASE)
    ext = (ext_m.group(1) if ext_m else "jpg").lower()
    lesson_dir = IMAGES_DIR / lesson.id
    lesson_dir.mkdir(parents=True, exist_ok=True)
    fname = f"quiz_{qid}.{ext}"
    out_path = lesson_dir / fname
    headers = {"Referer": lesson.url, "User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
            r = await client.get(url)
            r.raise_for_status()
            out_path.write_bytes(r.content)
            q["imageLocal"] = f"images/{lesson.id}/{fname}"
    except Exception as exc:
        print(f"    quiz img skip: {exc}")


_LANG_LABEL = {
    "en": "English", "fr": "French", "es": "Spanish",
    "pt": "Portuguese", "de": "German", "it": "Italian", "nl": "Dutch",
}

_LANG_MARKERS: dict[str, list[str]] = {
    "en": [r"\bthe\b", r"\band\b", r"\bthis\b", r"\bthat\b", r"\bare\b",
           r"\byou\b", r"\bis\b", r"\bof\b", r"\bwith\b", r"\bfor\b"],
    "fr": [r"\bce\b", r"\bvoyage\b", r"\bnous\b", r"\bvous\b", r"\bcette\b",
           r"\bdans\b", r"\bsont\b", r"\btous\b", r"\bessentiels\b", r"\béléments\b"],
    "es": [r"\bsigamos\b", r"\beste\b", r"\bvamos\b", r"\besenciales\b",
           r"\bcomponentes\b", r"\bconsola\b", r"\bcómo\b", r"\bestá\b"],
    "pt": [r"\bvamos\b", r"\bjornada\b", r"\bisso\b", r"\bessenciais\b",
           r"\bcomponentes\b", r"\btambém\b", r"\bnão\b", r"\bcontinuar\b"],
    "it": [r"\bche\b", r"\bquesto\b", r"\bquesta\b", r"\bviaggio\b",
           r"\bsono\b", r"\bessenziali\b", r"\bcomponenti\b", r"\bcomprensione\b"],
    "de": [r"\bder\b", r"\bdie\b", r"\bdas\b", r"\bund\b", r"\bnicht\b",
           r"\bist\b", r"\bsind\b", r"\bauf\b"],
    "nl": [r"\bhet\b", r"\bvan\b", r"\bvoor\b", r"\bniet\b", r"\bnaar\b"],
}


def _vtt_cue_text(text: str) -> str:
    """Extract the spoken text from a WebVTT — drop headers, timings, cue ids."""
    cue_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("WEBVTT", "NOTE")) or "-->" in s or s.isdigit():
            continue
        cue_lines.append(s)
        if len(cue_lines) > 80:
            break
    return " ".join(cue_lines)


def _is_storyboard(text: str) -> bool:
    """A 'subtitle' track whose cues are mostly URLs/image refs is a thumbnail
    storyboard, not real captions. Dyntube uses /story/, Circle puts mosaic refs."""
    body = _vtt_cue_text(text)
    if not body:
        return True
    # If most lines look like URLs / image fragments, it's a storyboard
    samples = [s for s in body.split(" ") if s][:30]
    url_like = sum(
        1 for s in samples
        if s.startswith("http")
        or s.startswith("pubsrv/")
        or "#xywh=" in s
        or s.endswith((".jpg", ".png"))
    )
    return url_like >= max(3, len(samples) // 2)


def _detect_vtt_lang(text: str) -> tuple[str, str]:
    """Return (lang_code, label) — scores all languages and picks the best."""
    body = _vtt_cue_text(text).lower()
    if not body:
        return "", "Captions"

    scores: dict[str, int] = {lang: 0 for lang in _LANG_MARKERS}
    for lang, patterns in _LANG_MARKERS.items():
        for p in patterns:
            scores[lang] += len(re.findall(p, body))

    # Distinctive characters
    if "ñ" in body: scores["es"] += 5
    if "ã" in body or "õ" in body: scores["pt"] += 5
    if "ß" in body: scores["de"] += 5
    if any(c in body for c in "äöü"): scores["de"] += 1
    # Romance: French has frequent é, è, à; Italian has fewer accents
    if re.search(r"[éèàêç]", body): scores["fr"] += 1
    if re.search(r"\bgg|\btt", body): scores["it"] += 1

    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best, _LANG_LABEL[best]
    return "", "Captions"


async def _download_inline_images(lesson: Lesson) -> None:
    """Find every <img src> in lesson.content_html, download to images/<id>/, rewrite."""
    if not lesson.content_html:
        return
    urls = re.findall(
        r'<img[^>]+src=["\'](https?://[^"\']+)["\']',
        lesson.content_html,
        re.IGNORECASE,
    )
    if not urls:
        return
    lesson_dir = IMAGES_DIR / lesson.id
    lesson_dir.mkdir(parents=True, exist_ok=True)

    headers = {"Referer": lesson.url, "User-Agent": USER_AGENT}
    url_to_local: dict[str, str] = {}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        for i, url in enumerate(set(urls)):
            ext_m = re.search(r'\.(jpg|jpeg|png|gif|webp|svg)', url, re.IGNORECASE)
            ext = (ext_m.group(1) if ext_m else "jpg").lower()
            fname = f"{i:02d}.{ext}"
            out_path = lesson_dir / fname
            if out_path.exists():
                url_to_local[url] = f"images/{lesson.id}/{fname}"
                continue
            try:
                r = await client.get(url)
                r.raise_for_status()
                out_path.write_bytes(r.content)
                url_to_local[url] = f"images/{lesson.id}/{fname}"
            except Exception as exc:
                print(f"    img skip: {exc}")

    # Rewrite img srcs in content_html to local paths
    def _replace(m: re.Match) -> str:
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        local = url_to_local.get(url)
        return f'{attr}={quote}/{local}{quote}' if local else m.group(0)

    lesson.content_html = re.sub(
        r'(src|data-src)=(["\'])(https?://[^"\']+)\2',
        _replace,
        lesson.content_html,
    )
    if url_to_local:
        print(f"    {len(url_to_local)} inline image(s) downloaded")


async def _download_subtitles(lesson: Lesson, urls: list[str]) -> None:
    """Download captured .vtt subtitle tracks; skip storyboard cues; label by language.

    Dyntube exposes a `/story/` track that's actually thumbnail-scrubber data,
    not subtitles — we discard those. Real captions come from `/cc/`.
    """
    if not urls:
        return
    lesson_dir = SUBS_DIR / lesson.id
    lesson_dir.mkdir(parents=True, exist_ok=True)

    # Dedupe by URL path (strip query string), and drop storyboard tracks
    seen_paths: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        path = u.split("?", 1)[0]
        if "/story/" in path:
            continue  # storyboard — not real subtitles
        if path not in seen_paths:
            seen_paths.add(path)
            deduped.append(u)

    headers = {"Referer": "https://videos.dyntube.com/", "User-Agent": USER_AGENT}
    out_subs: list[Subtitle] = []
    seen_langs: set[str] = set()
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        for i, url in enumerate(deduped):
            try:
                r = await client.get(url)
                r.raise_for_status()
                text = r.text
            except Exception as exc:
                print(f"    sub skip: {exc}")
                continue
            # Filter out storyboard tracks (thumbnail-strip data, not captions)
            if _is_storyboard(text):
                continue
            lang, label = _detect_vtt_lang(text)
            tag = lang or f"track{i}"
            fname = f"{tag}.vtt"
            if lang and lang in seen_langs:
                fname = f"{tag}_{i}.vtt"
            seen_langs.add(lang)
            out_path = lesson_dir / fname
            out_path.write_text(text, encoding="utf-8")
            out_subs.append(Subtitle(
                label=label,
                file=f"subtitles/{lesson.id}/{fname}",
                lang=lang or "en",
                default=(lang == "en"),
            ))

    # If we found English, make sure exactly one is default
    if any(s.lang == "en" for s in out_subs):
        for s in out_subs:
            s.default = (s.lang == "en") and not any(
                t.lang == "en" and t is not s and t.default for t in out_subs
            )
        # Ensure exactly one default
        en_subs = [s for s in out_subs if s.lang == "en"]
        for s in en_subs[1:]:
            s.default = False

    lesson.subtitles = out_subs
    if out_subs:
        labels = ", ".join(s.label for s in out_subs)
        print(f"    {len(out_subs)} subtitle track(s): {labels}")


async def _download_thumbnail(lesson: Lesson, url: str) -> None:
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{lesson.id}.jpg"
    out_path = THUMBS_DIR / fname
    if out_path.exists():
        lesson.thumb_file = f"thumbs/{fname}"
        return
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            out_path.write_bytes(r.content)
            lesson.thumb_file = f"thumbs/{fname}"
    except Exception as exc:
        print(f"    thumb skip: {exc}")


# ---------- video download ----------

def _download_dyntube(lesson: Lesson, out_path: Path) -> Optional[str]:
    """Download a Dyntube HLS video using captured key bytes + ffmpeg.

    Dyntube's HLS uses AES-128. The key URL embedded in the m3u8 returns 400
    when fetched outside the browser (it requires a session-specific URL).
    We sniffed the key bytes during scraping and saved them to KEYS_DIR;
    this function rewrites the m3u8 to point at the local key, then runs ffmpeg.
    """
    key_path = KEYS_DIR / f"{lesson.id}.key"
    if not key_path.exists():
        print(f"    no captured key for lesson {lesson.id} — cannot decrypt")
        return None

    headers = {"Referer": "https://videos.dyntube.com/", "User-Agent": USER_AGENT}
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
            # 1. Fetch master playlist → pick highest-quality variant
            r = client.get(lesson.video_url)
            r.raise_for_status()
            master = r.text
            quality_url = None
            best_h = -1
            for m in re.finditer(
                r'#EXT-X-STREAM-INF:[^\n]*RESOLUTION=\d+x(\d+)[^\n]*\n([^\n]+)',
                master,
            ):
                h = int(m.group(1))
                if h > best_h:
                    best_h = h
                    quality_url = m.group(2).strip()
            if not quality_url:
                print(f"    no quality variant in master m3u8")
                return None

            # 2. Fetch quality m3u8
            r = client.get(quality_url)
            r.raise_for_status()
            quality = r.text

            # 3. Rewrite EXT-X-KEY URI → file:// path for our captured key,
            #    and rewrite relative segment paths → absolute URLs.
            base = quality_url.rsplit("/", 1)[0] + "/"
            local_key_uri = "file://" + str(key_path.absolute())
            rewritten_lines: list[str] = []
            for line in quality.splitlines():
                if line.startswith("#EXT-X-KEY"):
                    line = re.sub(r'URI="[^"]*"', f'URI="{local_key_uri}"', line)
                elif line and not line.startswith("#"):
                    if not line.startswith("http"):
                        line = base + line
                rewritten_lines.append(line)

            HLS_DIR.mkdir(parents=True, exist_ok=True)
            local_m3u8 = HLS_DIR / f"{lesson.id}.m3u8"
            local_m3u8.write_text("\n".join(rewritten_lines), encoding="utf-8")
    except Exception as exc:
        print(f"    HLS prep failed: {type(exc).__name__}: {exc}")
        return None

    # 4. Run yt-dlp on the local m3u8 — supports --concurrent-fragments
    # which gives an ~8x speedup vs ffmpeg's serial segment fetch.
    # Segment URLs are signed (md5+expires) and don't need Referer/cookies.
    cmd = [
        "yt-dlp",
        "--enable-file-urls",        # allow file:// for local m3u8 + key
        "--concurrent-fragments", "8",
        "--no-playlist",
        "-o", str(out_path),
        f"file://{local_m3u8.absolute()}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print(f"    yt-dlp timeout")
        return None
    if result.returncode != 0:
        last = (result.stderr.strip().splitlines() or ["yt-dlp failed"])[-1]
        print(f"    yt-dlp error: {last[:200]}")
        return None
    if out_path.exists() and out_path.stat().st_size > 0:
        return f"videos/{out_path.name}"
    # yt-dlp may have used a different extension (.ts) — pick it up
    for p in sorted(VIDEOS_DIR.glob(f"{out_path.stem}.*")):
        if p.stat().st_size > 0:
            return f"videos/{p.name}"
    return None


def _download_video(lesson: Lesson, cookies_file: str) -> Optional[str]:
    """Download video — dispatches by lesson type (Circle = yt-dlp, Dyntube = ffmpeg+key)."""
    if not lesson.video_url:
        return None

    slug = _sanitize(lesson.title)
    fname = f"{lesson.lesson_index:03d}_{slug}.mp4"
    out_path = VIDEOS_DIR / fname
    if out_path.exists():
        return f"videos/{fname}"

    # Dyntube needs the captured AES key — handle it specially.
    if lesson.type == LessonType.VIDEO_DYNTUBE.value and "dyntube" in lesson.video_url:
        return _download_dyntube(lesson, out_path)

    # Circle (and anything else) goes through yt-dlp
    referer = lesson.url
    origin = "https://campus.petetong-djacademy.com"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--concurrent-fragments", "4",
        "--cookies", cookies_file,
        "--add-header", f"Referer:{referer}",
        "--add-header", f"Origin:{origin}",
        "--add-header", f"User-Agent:{USER_AGENT}",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        lesson.video_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print(f"    yt-dlp timeout on {fname}")
        return None
    if result.returncode != 0:
        last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp failed"
        print(f"    yt-dlp error: {last[:200]}")
        return None
    if out_path.exists():
        return f"videos/{fname}"
    for p in sorted(VIDEOS_DIR.glob(f"{lesson.lesson_index:03d}_{slug}.*")):
        return f"videos/{p.name}"
    return None


# ---------- attachment download ----------

async def _download_attachments(lesson: Lesson) -> None:
    """Download files. Attachments arrive with their URL stashed in `file`; replace with relative path."""
    if not lesson.attachments:
        return
    lesson_dir = FILES_DIR / lesson.id
    lesson_dir.mkdir(parents=True, exist_ok=True)
    final: list[Attachment] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        for att in lesson.attachments:
            url = att.file
            if not url or not url.startswith("http"):
                final.append(att)
                continue
            # Strip trailing file-size suffix that Circle merges into link text with
            # no separator (e.g. "beatport chart.pdf905.52 KB" → "beatport chart.pdf").
            cleaned = re.sub(r'\d[\d.,]*\s*(?:B|KB|MB|GB)\s*$', '', att.name or '', flags=re.IGNORECASE).strip()
            raw_name = cleaned or url.split("/")[-1].split("?")[0] or "file"
            ext_m = re.search(r'(\.[a-zA-Z0-9]{2,5})(?:\?.*)?$', url)
            ext = ext_m.group(1).lower() if ext_m else ""
            safe = _sanitize(raw_name.rsplit(".", 1)[0]) + (ext or "")
            out_path = lesson_dir / safe
            if out_path.exists():
                final.append(Attachment(
                    name=raw_name, file=f"files/{lesson.id}/{safe}", size=att.size,
                ))
                continue
            try:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                out_path.write_bytes(r.content)
                size_str = att.size or f"{len(r.content) // 1024} KB"
                final.append(Attachment(
                    name=raw_name, file=f"files/{lesson.id}/{safe}", size=size_str,
                ))
                print(f"    file {safe} ({size_str})")
            except Exception as exc:
                print(f"    file skip {raw_name}: {exc}")
                final.append(Attachment(name=raw_name, file="", size=att.size))
    lesson.attachments = final


# ---------- writer ----------

def _write_manifest(lessons: list[Lesson]) -> None:
    data = [l.to_manifest() for l in lessons]
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
    )


# ---------- walker (per-lesson processing) ----------

async def _click_and_verify_complete(page) -> bool:
    """Click 'Complete lesson' button and verify by reloading the page.

    Circle's backend can take 5-10s to persist completion, and the on-page
    button text doesn't update until a reload. Click → wait → reload → verify.
    """
    try:
        await page.click(
            'button[type="submit"]:has-text("Complete lesson")',
            timeout=8000,
        )
    except Exception:
        return False
    # Give the backend write room to land
    await page.wait_for_timeout(6000)
    # Reload and check that the button now says "Completed"
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    try:
        await page.wait_for_selector(CONTENT_SELECTOR, timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)
    state = await page.evaluate("""
        () => {
            for (const b of document.querySelectorAll('button[type="submit"]')) {
                const t = (b.textContent || '').trim().toLowerCase();
                if (t === 'completed') return 'completed';
                if (t === 'complete lesson') return 'pending';
            }
            return 'none';
        }
    """)
    return state == "completed"


async def _ensure_completed(ctx, prev: Lesson) -> str:
    """Open `prev` and force its completion. Returns:
       'completed' — was already, or click verified
       'pending'   — clicked but verification timed out
       'locked'    — page shows locked itself; previous-previous needs work
       'none'      — no button visible (unknown state)
    """
    page = await ctx.new_page()
    page.set_default_timeout(30000)
    try:
        await page.goto(prev.url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector(CONTENT_SELECTOR, timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)

        state = await page.evaluate("""
            () => {
                const body = document.querySelector('[class*="mx-auto"][class*="max-w-"][class*="space-y-8"]');
                const txt = body ? (body.innerText || '').trim() : '';
                if (/lesson locked/i.test(txt)) return { kind: 'locked' };
                for (const b of document.querySelectorAll('button[type="submit"]')) {
                    const t = (b.textContent || '').trim().toLowerCase();
                    if (t === 'completed') return { kind: 'completed' };
                    if (t === 'complete lesson') return { kind: 'pending' };
                }
                return { kind: 'none' };
            }
        """)
        kind = state.get("kind", "none")

        if kind == "completed":
            return "completed"
        if kind == "locked":
            return "locked"
        if kind == "pending":
            ok = await _click_and_verify_complete(page)
            return "completed" if ok else "pending"
        return "none"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _process_lesson(ctx, lesson: Lesson) -> None:
    """Open a fresh page, navigate to the lesson, classify + extract + maybe complete."""
    page = await ctx.new_page()
    page.set_default_timeout(30000)

    # Network sniffer captures:
    #   * video manifest/file URLs (master m3u8, quality m3u8, mp4)
    #   * AES key bytes (Dyntube's player uses a session-specific /player-hls-key
    #     URL that doesn't work outside the browser context — capture the bytes
    #     while we're in the right context)
    #   * subtitle .vtt URLs (Dyntube serves multiple language tracks)
    video_urls: list[str] = []
    subtitle_urls: list[str] = []
    key_bytes: list[bytes] = []  # use list to mutate from inner async callback

    async def grab_key_bytes(resp: Response):
        try:
            data = await resp.body()
            if data and 8 <= len(data) <= 64 and not key_bytes:
                key_bytes.append(data)
        except Exception:
            pass  # TargetClosedError etc. — page closed before body was read

    def on_response(resp: Response):
        try:
            url = resp.url
            ct = resp.headers.get("content-type", "")
            is_hls = (
                ".m3u8" in url
                or "hls-master" in url
                or "application/vnd.apple.mpegurl" in ct
            )
            is_mp4 = re.search(r"\.mp4(\?|$)", url, re.IGNORECASE) or "video/mp4" in ct
            is_vtt = ".vtt" in url or "text/vtt" in ct
            relevant_host = any(
                d in url for d in (
                    "dyntube.com", "dyntube.net", "cdn-media.circle.so",
                )
            )
            if relevant_host and (is_hls or is_mp4):
                if url not in video_urls:
                    video_urls.append(url)
            if relevant_host and is_vtt and url not in subtitle_urls:
                subtitle_urls.append(url)
            # Capture AES key bytes — Dyntube serves these via /player-hls-key/
            if "/player-hls-key/" in url or "/hls-key/" in url:
                t = asyncio.create_task(grab_key_bytes(resp))
                # Swallow the future's exception so it never surfaces as
                # "Future exception was never retrieved" after page close.
                t.add_done_callback(lambda f: None)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        try:
            await page.goto(lesson.url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            lesson.error = f"goto: {type(exc).__name__}: {exc}"
            return

        # Wait for the lesson body to render — selector visible AND has substantial text
        try:
            await page.wait_for_selector(CONTENT_SELECTOR, timeout=15000)
        except Exception:
            pass
        # Wait for the body to contain actual content (not just an empty container).
        # "Lesson locked" pages have ~80 chars; real content has much more.
        try:
            await page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const txt = (el.innerText || '').trim();
                    return txt.length > 60;
                }""",
                arg=CONTENT_SELECTOR,
                timeout=10000,
            )
        except Exception:
            pass
        # Give Circle/Dyntube player time to attach video sources
        await page.wait_for_timeout(8000)

        try:
            signals = await page.evaluate(SIGNAL_JS)
        except Exception as exc:
            lesson.error = f"signals: {type(exc).__name__}: {exc}"
            return

        # Use the live h2 title if we got one
        if signals.get("title"):
            lesson.title = signals["title"]

        # Classify (DOM + network-sniffed URLs)
        lesson_type = classify(signals, lesson.title, network_urls=video_urls)
        lesson.type = lesson_type.value

        if lesson_type == LessonType.LOCKED:
            print(f"    LOCKED — skipping (will retry after prev unlocks)")
            return

        # For Dyntube videos: trigger play inside the iframe so the network
        # sniffer captures the real m3u8/mp4 URLs (Dyntube doesn't auto-load
        # the video file; it waits for user interaction).
        if lesson_type == LessonType.VIDEO_DYNTUBE:
            for f in page.frames:
                if "dyntube" in f.url:
                    try:
                        await f.evaluate(
                            "() => { const v = document.querySelector('video'); if (v) v.play(); }"
                        )
                    except Exception:
                        pass
                    break
            # Give the player ~6s to request the master playlist + first segment
            await page.wait_for_timeout(6000)

        # Persist any captured AES key bytes — needed for Dyntube HLS decrypt
        if key_bytes:
            KEYS_DIR.mkdir(parents=True, exist_ok=True)
            (KEYS_DIR / f"{lesson.id}.key").write_bytes(key_bytes[0])

        # Extract
        await _extract_lesson(page, lesson, signals, video_urls)

        # Download inline images and rewrite contentHtml to local paths.
        # Signed CDN URLs expire — must do this in the same session.
        await _download_inline_images(lesson)

        # Download subtitles inline — signed URLs expire fast, must do now
        if subtitle_urls:
            await _download_subtitles(lesson, subtitle_urls)

        # If unlocked + not yet completed, click "Complete lesson" to unlock the next.
        # The click writes to the backend; that's what unlocks the next lesson —
        # the visible button text doesn't always update before navigation, so we
        # don't gate on it.
        cb = signals.get("complete_button") or {}
        cb_text = (cb.get("text") or "").strip().lower()
        if cb_text == "complete lesson":
            # After a quiz is passed Circle temporarily disables the "Complete lesson"
            # button while it processes the result. Wait up to 20s for it to re-enable
            # before attempting the click.
            if lesson.type == LessonType.QUIZ.value:
                # After passing a quiz, Circle auto-marks the lesson complete on its
                # backend and leaves the "Complete lesson" button permanently disabled
                # as a non-interactive indicator. Check if the button is disabled; if
                # so, treat the lesson as already completed without clicking.
                quiz_btn_disabled = await page.evaluate("""
                    () => {
                        for (const b of document.querySelectorAll('button[type="submit"]')) {
                            if ((b.textContent || '').trim().toLowerCase() === 'complete lesson')
                                return b.disabled;
                        }
                        return false;
                    }
                """)
                if quiz_btn_disabled:
                    lesson.completed = True
                    print(f"    marked complete (quiz auto-completed by platform)")
                    cb_text = ""  # skip the click below
            try:
                if cb_text == "complete lesson":
                    await page.click(
                        'button[type="submit"]:has-text("Complete lesson")',
                        timeout=8000,
                    )
                    # Backend takes 5-10s to persist. We trust the click — Circle's
                    # button text doesn't reliably update on the same page anyway,
                    # so verify-by-reload gives false negatives. If the chain is
                    # actually broken, the next lesson will hit LOCKED and trigger
                    # the walk-back recovery which uses the verified-click helper.
                    await page.wait_for_timeout(8000)
                    lesson.completed = True
                    print(f"    marked complete")
            except Exception as exc:
                print(f"    complete click failed: {type(exc).__name__}: {exc}")
        elif "completed" in cb_text:
            lesson.completed = True  # already completed
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ---------- commands ----------

async def cmd_login(course_url: str) -> None:
    print(f"Opening browser → navigate to: {course_url}")
    print("Log in, then close the window (or wait 5 minutes).")
    p_api, ctx = await _open_context(headless=False)
    page = await ctx.new_page()
    try:
        await page.goto(course_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(300_000)
    except Exception:
        pass
    finally:
        await _close_context(p_api, ctx)
    print("Session saved.")


async def _refresh_cookies_file(ctx) -> str:
    """Export current browser cookies to a temp Netscape file. Caller deletes."""
    raw = await ctx.cookies()
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(_cookies_to_netscape(raw))
    tmp.close()
    return tmp.name


async def cmd_download(course_url: str, limit: Optional[int] = None, dry_run: bool = False, lesson_ids: Optional[set] = None) -> None:
    for d in (VIDEOS_DIR, IMAGES_DIR, FILES_DIR, QUIZZES_DIR, THUMBS_DIR, SUBS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print("Opening headless browser…")
    p_api, ctx = await _open_context(headless=True)
    cookies_file = ""

    try:
        # Discovery
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        if "sign_in" in page.url or "login" in page.url:
            print("Not logged in — run: uv run helpers/download_course.py login <url>")
            sys.exit(1)
        print("Discovering lessons…")
        lessons = await _scrape_lesson_list(page, course_url)
        print(f"  found {len(lessons)} lessons")
        await page.close()
        if not lessons:
            print("No lessons found.")
            sys.exit(1)

        # Resume cache
        cache: dict[str, dict] = {}
        if MANIFEST_FILE.exists():
            try:
                for entry in json.loads(MANIFEST_FILE.read_text(encoding="utf-8")):
                    cache[entry["id"]] = entry
                print(f"  resume cache: {len(cache)} prior entries")
            except Exception:
                pass

        # Keep all lessons in the list (so manifest stays complete) but only
        # process up to `limit` of them. Past `limit`, we skip processing but
        # still merge cached state so the manifest preserves prior data.
        process_count = limit if limit is not None else len(lessons)
        if limit is not None:
            print(f"  --limit {limit}: processing first {limit} (full manifest preserved)")

        # Cookies file for yt-dlp — exported once, refreshed if it gets stale.
        if not dry_run:
            cookies_file = await _refresh_cookies_file(ctx)

        # Per-lesson: scrape + classify + (in same beat) download video
        # Tokens for Dyntube/Circle expire fast — must download in same session.
        for i, lesson in enumerate(lessons, 1):
            cached = cache.get(lesson.id) or {}
            # Always merge cached state into the lesson so the manifest stays
            # complete even when --limit caps the active processing count.
            if cached:
                lesson.type = cached.get("type", lesson.type)
                lesson.extracted = cached.get("extracted", False)
                lesson.completed = cached.get("completed", False)
                lesson.video_url = cached.get("videoUrl") or cached.get("video_url")
                lesson.video_file = cached.get("videoFile") or cached.get("video_file")
                lesson.thumb_file = cached.get("thumbFile") or cached.get("thumb_file")
                lesson.content_html = cached.get("contentHtml") or cached.get("content_html") or ""
                lesson.quiz_file = cached.get("quizFile") or cached.get("quiz_file")
                lesson.attachments = [
                    Attachment(name=a.get("name", ""), file=a.get("file", ""), size=a.get("size", ""))
                    for a in cached.get("attachments", []) or []
                ]
                lesson.subtitles = [
                    Subtitle(
                        label=s.get("label", ""), file=s.get("file", ""),
                        lang=s.get("lang", "en"), default=s.get("default", False),
                    )
                    for s in cached.get("subtitles", []) or []
                ]
                # Skip if fully done — already has video file (or doesn't need one).
                # --lesson-ids bypasses the skip so targeted lessons are re-scraped.
                non_video = lesson.type not in (
                    LessonType.VIDEO_CIRCLE.value, LessonType.VIDEO_DYNTUBE.value,
                )
                force = lesson_ids is not None and lesson.id in lesson_ids
                if not force and lesson.extracted and lesson.completed and (non_video or lesson.video_file):
                    print(f"  [{i:03d}/{len(lessons)}] {lesson.title[:55]} (cached: {lesson.type})")
                    continue

            # Past --limit, just keep cached state — don't process further.
            if i > process_count:
                continue

            # --lesson-ids: skip anything not in the target set.
            if lesson_ids is not None and lesson.id not in lesson_ids:
                continue

            print(f"  [{i:03d}/{len(lessons)}] {lesson.title[:55]}", flush=True)
            # Quiz lessons need longer: brute-forcing N questions × M options
            # with 3s waits per submit easily exceeds 90s.
            lesson_timeout = 300 if lesson.type == LessonType.QUIZ.value else 90
            try:
                await asyncio.wait_for(_process_lesson(ctx, lesson), timeout=lesson_timeout)
            except asyncio.TimeoutError:
                lesson.error = f"timeout {lesson_timeout}s"
                print(f"    timeout {lesson_timeout}s — moving on")
            except Exception as exc:
                lesson.error = f"{type(exc).__name__}: {exc}"
                print(f"    error: {lesson.error}")

            # If the page was LOCKED, walk back through previous lessons until
            # we find one that's pending-but-clickable, complete it (verified),
            # then walk forward completing any others, then retry the current.
            if lesson.type == LessonType.LOCKED.value and i >= 2:
                print(f"    LOCKED — walking back to find pending completion", flush=True)
                walked: list[int] = []
                for j in range(i - 2, -1, -1):  # i-2 down to 0 (0-based prev indices)
                    prev = lessons[j]
                    res = await _ensure_completed(ctx, prev)
                    print(f"      #{j + 1} {prev.title[:40]}: {res}", flush=True)
                    if res in ("completed", "pending"):
                        # Found the chain anchor — replay forward sequentially.
                        # "pending" means the button was already clicked (verification
                        # timed out) — still the right place to stop walking back.
                        # Stop replaying forward as soon as a lesson is still locked;
                        # the chain is sequential and each lesson only unlocks after
                        # the previous one is confirmed on the server.
                        for k in range(j + 1, i - 1):
                            mid = lessons[k]
                            res2 = await _ensure_completed(ctx, mid)
                            print(f"      #{k + 1} {mid.title[:40]}: {res2}", flush=True)
                            walked.append(k)
                            if res2 == "locked":
                                break  # chain still blocked here — stop
                        break
                    walked.append(j)
                # Retry current lesson
                lesson.error = None
                lesson.type = LessonType.UNKNOWN.value
                try:
                    await asyncio.wait_for(_process_lesson(ctx, lesson), timeout=90)
                    if lesson.type != LessonType.LOCKED.value:
                        print(f"    recovered: type={lesson.type} completed={lesson.completed}",
                              flush=True)
                except asyncio.TimeoutError:
                    lesson.error = "retry timeout"
                except Exception as exc:
                    lesson.error = f"retry {type(exc).__name__}: {exc}"

                # Inline downloads if recovery yielded video/url
                if (
                    not dry_run
                    and lesson.video_url
                    and not lesson.video_file
                    and lesson.type in (LessonType.VIDEO_CIRCLE.value, LessonType.VIDEO_DYNTUBE.value)
                ):
                    print(f"    downloading video (post-recovery)…", flush=True)
                    lesson.video_file = _download_video(lesson, cookies_file)
                    if lesson.video_file:
                        print(f"    saved → {lesson.video_file}")
            print(f"    type={lesson.type} extracted={lesson.extracted} "
                  f"completed={lesson.completed} video_url={'Y' if lesson.video_url else '-'}")

            # Inline video download — must happen NOW while signed URLs are fresh
            if (
                not dry_run
                and lesson.video_url
                and not lesson.video_file
                and lesson.type in (LessonType.VIDEO_CIRCLE.value, LessonType.VIDEO_DYNTUBE.value)
            ):
                print(f"    downloading video…", flush=True)
                lesson.video_file = _download_video(lesson, cookies_file)
                if lesson.video_file:
                    print(f"    saved → {lesson.video_file}")

            # Inline attachment download — also can have signed URLs
            if not dry_run and lesson.attachments:
                await _download_attachments(lesson)

            _write_manifest(lessons)

    finally:
        await _close_context(p_api, ctx)
        if cookies_file:
            try:
                os.unlink(cookies_file)
            except Exception:
                pass

    if dry_run:
        print("\n=== DRY RUN — skipping video/file downloads ===")

    _write_manifest(lessons)
    print(f"\nManifest: {MANIFEST_FILE} ({len(lessons)} lessons)")
    print(f"Data: {COURSE_DIR}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, url = sys.argv[1], sys.argv[2]
    extra = sys.argv[3:]
    limit = None
    dry_run = False
    lesson_ids = None
    i = 0
    while i < len(extra):
        if extra[i] == "--limit" and i + 1 < len(extra):
            limit = int(extra[i + 1]); i += 2
        elif extra[i] == "--dry-run":
            dry_run = True; i += 1
        elif extra[i] == "--lesson-ids" and i + 1 < len(extra):
            lesson_ids = set(extra[i + 1].split(",")); i += 2
        else:
            print(f"Unknown arg: {extra[i]}"); sys.exit(1)

    if cmd == "login":
        asyncio.run(cmd_login(url))
    elif cmd == "download":
        log_p, log_fh = open_log("download-course")
        orig_stdout = sys.stdout
        sys.stdout = _Tee(orig_stdout, log_fh)
        print(f"Log: {log_p}")
        try:
            with caffeinate():
                asyncio.run(cmd_download(url, limit=limit, dry_run=dry_run, lesson_ids=lesson_ids))
        finally:
            sys.stdout = orig_stdout
            log_fh.close()
    else:
        print(f"Unknown command: {cmd!r}"); sys.exit(1)


if __name__ == "__main__":
    main()
