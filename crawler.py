"""Async Playwright crawler.

BFS from a seed URL, same-origin only, yielding one ``CrawlUnit`` per fetched
page. Pages that render the same layout (per ``digest.dom_shape_signature``)
are still fetched (so the pipeline can record every page hit), but only the
first page for each distinct template is flagged ``is_new_template=True`` --
downstream, expensive LLM tiers should only act on those.

URL-pattern dedup (numeric/uuid path segments collapsed to ``:id``) keeps the
crawl from wasting its budget walking every ``/p/1``, ``/p/2``, ... before it
ever gets a chance to see the rest of the site.
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from config import (
    CRAWL_CONCURRENCY,
    MAX_DEPTH,
    MAX_PAGES,
    MAX_TEMPLATES,
    NAV_TIMEOUT_MS,
)
from digest import dom_shape_signature

# ---------------------------------------------------------------------------
# browser launch (this container's driver/browser pairing can mismatch --
# fall back to the newest Playwright-managed binary found on disk)
# ---------------------------------------------------------------------------

_FALLBACK_GLOB = "chromium-*/chrome-linux/chrome"


async def launch_browser(pw):
    """Launch headless chromium, tolerating a driver/browser version
    mismatch by falling back to the newest Playwright-managed binary found
    under PLAYWRIGHT_BROWSERS_PATH (or /opt/pw-browsers). Exported so other
    modules (e.g. pipeline_flow) can reuse the same fallback."""
    try:
        return await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    except Exception:
        root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers"
        candidates = sorted(glob.glob(os.path.join(root, _FALLBACK_GLOB)))
        if not candidates:
            raise
        return await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path=candidates[-1],
        )


# ---------------------------------------------------------------------------
# CrawlUnit
# ---------------------------------------------------------------------------


@dataclass
class CrawlUnit:
    url: str
    final_url: str
    status: int
    resp_headers: dict
    tls: Optional[dict]
    html: str
    template_id: str
    is_new_template: bool
    depth: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# link filtering / URL-pattern dedup helpers
# ---------------------------------------------------------------------------

_BINARY_EXTS = (
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".mp4", ".mp3", ".avi", ".mov", ".wav", ".ogg",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".eot",
)

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "#")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_NUMERIC_RE = re.compile(r"^\d+$")


def _origin(url: str) -> tuple:
    parts = urlsplit(url)
    scheme = parts.scheme
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, (parts.hostname or "").lower(), port)


def _is_binary(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(_BINARY_EXTS)


def _extract_links(html: str, base_url: str) -> list:
    """a[href] -> absolute, fragment-stripped URLs, skipping mailto/tel/js
    and obvious binaries. Not origin-filtered here -- callers do that."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.lower().startswith(_SKIP_SCHEMES):
            continue
        try:
            resolved = urljoin(base_url or "", href)
        except ValueError:
            continue
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https"):
            continue
        resolved = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if _is_binary(resolved):
            continue
        links.append(resolved)
    return links


def _url_pattern_key(url: str) -> str:
    """Path-shape key: numeric / uuid-like path segments collapsed to
    ``:id`` (query/fragment ignored) -- ``/p/1`` and ``/p/2`` share a key."""
    parts = urlsplit(url)
    segments = parts.path.split("/")
    normalized = [
        ":id" if (_NUMERIC_RE.match(seg) or _UUID_RE.match(seg)) else seg
        for seg in segments
    ]
    return "%s://%s%s" % (parts.scheme, parts.netloc.lower(), "/".join(normalized))


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


async def crawl(
    seed_url,
    *,
    max_pages=None,
    max_depth=None,
    max_templates=None,
    browser=None,
) -> AsyncIterator[CrawlUnit]:
    """BFS same-origin crawl from ``seed_url``, yielding one ``CrawlUnit``
    per fetched page. Stops once ``max_pages`` pages have been fetched or
    ``max_templates`` distinct templates have been seen, whichever first.

    If ``browser`` is passed in it is reused and left open for the caller;
    otherwise a browser is launched (via ``launch_browser``) and closed in a
    ``finally`` block.
    """
    max_pages = MAX_PAGES if max_pages is None else max_pages
    max_depth = MAX_DEPTH if max_depth is None else max_depth
    max_templates = MAX_TEMPLATES if max_templates is None else max_templates

    seed_origin = _origin(seed_url)
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)

    owns_browser = browser is None
    pw = None
    if owns_browser:
        pw = await async_playwright().start()
        browser = await launch_browser(pw)

    context = await browser.new_context()

    async def _fetch(url: str, depth: int) -> dict:
        async with sem:
            page = await context.new_page()
            try:
                try:
                    resp = await page.goto(
                        url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                    )
                except Exception as exc:
                    return {"url": url, "depth": depth, "error": str(exc)}

                final_url = page.url
                status = resp.status if resp is not None else 0

                headers = {}
                if resp is not None:
                    try:
                        raw_headers = await resp.all_headers()
                        headers = {str(k).lower(): v for k, v in raw_headers.items()}
                    except Exception:
                        headers = {}

                tls = None
                if resp is not None:
                    try:
                        tls = await resp.security_details()
                    except Exception:
                        tls = None

                try:
                    html = await page.content()
                except Exception:
                    html = ""

                return {
                    "url": url,
                    "depth": depth,
                    "error": None,
                    "final_url": final_url,
                    "status": status,
                    "headers": headers,
                    "tls": dict(tls) if tls is not None else None,
                    "html": html,
                }
            finally:
                await page.close()

    try:
        visited_patterns = {_url_pattern_key(seed_url)}
        seen_templates = set()
        pages_fetched = 0
        stopped = False

        current_wave = [(seed_url, 0)]

        while current_wave and not stopped:
            coros = [_fetch(url, depth) for url, depth in current_wave]
            next_wave = []

            for coro in asyncio.as_completed(coros):
                result = await coro
                url = result["url"]
                depth = result["depth"]

                if result.get("error") is not None:
                    yield CrawlUnit(
                        url=url,
                        final_url=url,
                        status=0,
                        resp_headers={},
                        tls=None,
                        html="",
                        template_id="",
                        is_new_template=False,
                        depth=depth,
                        error=result["error"],
                    )
                    continue

                html = result["html"]
                template_id = dom_shape_signature(html)
                is_new_template = template_id not in seen_templates
                if is_new_template:
                    seen_templates.add(template_id)
                pages_fetched += 1

                yield CrawlUnit(
                    url=url,
                    final_url=result["final_url"],
                    status=result["status"],
                    resp_headers=result["headers"],
                    tls=result["tls"],
                    html=html,
                    template_id=template_id,
                    is_new_template=is_new_template,
                    depth=depth,
                    error=None,
                )

                if pages_fetched >= max_pages or len(seen_templates) >= max_templates:
                    stopped = True
                    continue

                if depth >= max_depth:
                    continue

                for link in _extract_links(html, result["final_url"]):
                    if _origin(link) != seed_origin:
                        continue
                    pattern = _url_pattern_key(link)
                    if pattern in visited_patterns:
                        continue
                    visited_patterns.add(pattern)
                    next_wave.append((link, depth + 1))

            current_wave = [] if stopped else next_wave
    finally:
        await context.close()
        if owns_browser:
            await browser.close()
            await pw.stop()
