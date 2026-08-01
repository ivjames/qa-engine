"""Unit tests for crawler.py.

Serves a tiny in-process site (plain http.server on a background thread,
routes keyed by exact path) and drives the async crawler against it. No
pytest-asyncio is installed, so async work is driven with asyncio.run()
inside ordinary sync test functions, mirroring tests/unit/test_wcag.py.
"""

import asyncio
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from crawler import crawl, launch_browser

# ---------------------------------------------------------------------------
# fixture site
# ---------------------------------------------------------------------------

_ITEM_TMPL = (
    "<html><body><main class=\"item\">"
    "<h1>{title}</h1><p class=\"body\">{desc}</p>"
    "</main></body></html>"
)

ROUTES = {
    "/": (
        "<html><body><main><h1>Index</h1>"
        '<a href="/page2">Page 2</a>'
        # deliberately unreachable (TEST-NET-1, RFC 5737) + different origin:
        # same-origin filtering must drop this before any navigation is
        # attempted, so this link must never slow the test down.
        '<a href="http://192.0.2.1/external">External</a>'
        "</main></body></html>"
    ),
    "/page2": "<html><body><main><h1>Page 2</h1></main></body></html>",
    "/hub": (
        "<html><body><main><h1>Hub</h1>"
        '<a href="/item/apple">Apple</a>'
        '<a href="/item/banana">Banana</a>'
        '<a href="/item/cherry">Cherry</a>'
        "</main></body></html>"
    ),
    "/item/apple": _ITEM_TMPL.format(title="Apple", desc="A crisp fruit."),
    "/item/banana": _ITEM_TMPL.format(title="Banana", desc="A yellow fruit."),
    "/item/cherry": _ITEM_TMPL.format(title="Cherry", desc="A small red fruit."),
    "/paged/1": "<html><body><main><h1>Page one</h1></main></body></html>",
    "/paged/2": "<html><body><main><h1>Page two</h1></main></body></html>",
    "/paged/3": "<html><body><main><h1>Page three</h1></main></body></html>",
    "/pager": (
        "<html><body><main><h1>Pager</h1>"
        '<a href="/paged/1">1</a>'
        '<a href="/paged/2">2</a>'
        '<a href="/paged/3">3</a>'
        "</main></body></html>"
    ),
}


def _make_handler(routes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            body = routes.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args, **kwargs):  # silence test noise
            pass

    return Handler


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(ROUTES))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# async helper: launch via crawler's own container-safe fallback, run a full
# crawl to completion, collect every yielded CrawlUnit.
# ---------------------------------------------------------------------------


async def _crawl_list(seed_url, **kwargs):
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            units = []
            async for unit in crawl(seed_url, browser=browser, **kwargs):
                units.append(unit)
            return units
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_two_page_linked_site_yields_two_units(base_url):
    units = asyncio.run(
        _crawl_list(base_url + "/", max_depth=2, max_pages=50, max_templates=50)
    )

    assert len(units) == 2
    urls = {u.url for u in units}
    assert urls == {base_url + "/", base_url + "/page2"}
    for u in units:
        assert u.status == 200
        assert u.error is None
        assert u.html


def test_same_template_pages_dedup_after_first(base_url):
    units = asyncio.run(
        _crawl_list(base_url + "/hub", max_depth=2, max_pages=50, max_templates=50)
    )

    # hub + 3 item pages, all fetched (distinct URL-pattern keys since the
    # slugs aren't numeric/uuid, so none of them get skipped at enqueue time).
    assert len(units) == 4

    item_units = [u for u in units if "/item/" in u.url]
    assert len(item_units) == 3
    assert {u.url for u in item_units} == {
        base_url + "/item/apple",
        base_url + "/item/banana",
        base_url + "/item/cherry",
    }

    # all three item pages share one DOM shape -> one signature.
    assert len({u.template_id for u in item_units}) == 1
    # first one seen is flagged new, the other two are not (order among the
    # concurrent fetches isn't guaranteed, so check the count, not identity).
    new_flags = [u.is_new_template for u in item_units]
    assert new_flags.count(True) == 1
    assert new_flags.count(False) == 2


def test_same_origin_restriction_external_link_not_followed(base_url):
    units = asyncio.run(
        _crawl_list(base_url + "/", max_depth=3, max_pages=50, max_templates=50)
    )

    urls = {u.url for u in units}
    assert not any("192.0.2.1" in u for u in urls)
    assert not any(u.final_url and "192.0.2.1" in u.final_url for u in units)
    # only the two same-origin pages were ever fetched.
    assert urls == {base_url + "/", base_url + "/page2"}


def test_url_pattern_dedup_bounds_numeric_pagination(base_url):
    """Bonus coverage for the URL-pattern dedup rule itself: numeric path
    segments collapse to the same pattern key, so only the first of
    /paged/1, /paged/2, /paged/3 ever gets enqueued."""
    units = asyncio.run(
        _crawl_list(base_url + "/pager", max_depth=2, max_pages=50, max_templates=50)
    )

    paged_units = [u for u in units if "/paged/" in u.url]
    assert len(paged_units) == 1
