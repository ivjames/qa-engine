"""Real-browser test for tier0.wcag: launches headless chromium via
Playwright, feeds it a fixture page with planted accessibility violations,
and checks that run_axe() maps axe-core's output onto the canonical
finding-dict shape.

No pytest-asyncio is installed, so async work is driven with asyncio.run()
inside ordinary sync test functions.
"""

import asyncio
import glob
import os

import pytest
from playwright.async_api import async_playwright

from tier0 import wcag

FIXTURE_HTML = """
<!doctype html>
<html lang="en">
<head><title>Fixture page</title></head>
<body>
  <h1>Fixture</h1>
  <!-- image-alt violation: no alt attribute -->
  <img src="logo.png">

  <!-- color-contrast violation: light grey on white -->
  <p style="color:#dddddd;background-color:#ffffff;">
    This text has very low contrast against its background.
  </p>

  <!-- label violation: input with no associated label -->
  <input type="text" name="email">
</body>
</html>
"""

_FALLBACK_GLOB = "/opt/pw-browsers/chromium-*/chrome-linux/chrome"


async def _launch_chromium(playwright):
    """Launch headless chromium, tolerating a driver/browser version
    mismatch by falling back to the newest Playwright-managed binary found
    on disk."""
    try:
        return await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    except Exception:
        candidates = sorted(glob.glob(_FALLBACK_GLOB))
        if not candidates:
            raise
        return await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path=candidates[-1],
        )


async def _run_axe_on_fixture(monkeypatch_add_script_tag=False):
    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.set_content(FIXTURE_HTML)

            if monkeypatch_add_script_tag:
                async def _boom(*args, **kwargs):
                    raise RuntimeError("CSP blocked injected <script> tag")

                page.add_script_tag = _boom

            findings = await wcag.run_axe(page, "https://example.com/fixture")
            return findings
        finally:
            await browser.close()


def test_run_axe_finds_planted_violations():
    findings = asyncio.run(_run_axe_on_fixture())

    assert len(findings) >= 2

    rules = {f["rule"] for f in findings}
    assert "image-alt" in rules

    valid_severities = {"critical", "serious", "moderate", "minor", "info"}
    for f in findings:
        assert f["url"] == "https://example.com/fixture"
        assert f["pipeline"] == "wcag"
        assert f["tier"] == 0
        assert isinstance(f["rule"], str) and f["rule"]
        assert f["severity"] in valid_severities
        assert isinstance(f["title"], str) and f["title"]
        assert isinstance(f["detail"], str) and f["detail"]

        evidence = f["evidence"]
        assert isinstance(evidence, dict)
        assert "impact" in evidence
        assert "nodes" in evidence
        assert "node_count" in evidence
        assert "tags" in evidence
        assert isinstance(evidence["nodes"], list)
        assert len(evidence["nodes"]) <= 5
        for node in evidence["nodes"]:
            assert set(node.keys()) == {"selector", "snippet"}
            assert isinstance(node["selector"], str)
            assert isinstance(node["snippet"], str)
            assert len(node["snippet"]) <= 200

        # No secrets/PII should leak into evidence -- fixture has none, so
        # this is a smoke check that nothing unexpected got serialized in.
        blob = repr(evidence)
        for secret_marker in ("ANTHROPIC_API_KEY", "password", "secret_key"):
            assert secret_marker not in blob


def test_run_axe_image_alt_finding_shape():
    findings = asyncio.run(_run_axe_on_fixture())
    image_alt = [f for f in findings if f["rule"] == "image-alt"]
    assert len(image_alt) == 1

    finding = image_alt[0]
    assert finding["severity"] == "critical"
    assert finding["evidence"]["impact"] == "critical"
    assert finding["evidence"]["node_count"] >= 1
    assert "img" in finding["evidence"]["nodes"][0]["snippet"].lower()


def test_run_axe_evaluate_fallback_when_add_script_tag_fails():
    """CSP-style failure: add_script_tag raises, run_axe must fall back to
    page.evaluate() of the vendored source and still succeed."""
    findings = asyncio.run(_run_axe_on_fixture(monkeypatch_add_script_tag=True))

    assert len(findings) >= 2
    rules = {f["rule"] for f in findings}
    assert "image-alt" in rules
    # The fallback path must not itself report an injection failure.
    assert "axe-injection-failed" not in rules


def test_vendor_file_present_and_sane():
    assert os.path.isfile(wcag.VENDOR_PATH)
    size = os.path.getsize(wcag.VENDOR_PATH)
    assert 200_000 <= size <= 600_000

    with open(wcag.VENDOR_PATH, "r", encoding="utf-8") as f:
        head = f.read(200)
    assert head.startswith("/*! axe") or "axe-core" in head
