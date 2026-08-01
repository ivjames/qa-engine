"""Tier-0 UX pipeline: run Google Lighthouse (as a subprocess) against a
live URL and map its category scores + selected failing audits onto the
qa-engine canonical finding dict.

Deterministic/free (no LLM), which is why this lives at tier 0. Lighthouse
itself is a Node CLI (invoked via `npx --no-install lighthouse`) driving a
headless Chrome; both of those can be absent, slow, or flaky, so every
failure mode here degrades to a single informational finding instead of
raising.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

Finding = Dict[str, Any]

# Lighthouse audit ids we promote to their own findings when they score 0
# (or otherwise report failure). image-alt / label / color-contrast are
# deliberately EXCLUDED here even though Lighthouse's accessibility category
# includes them -- tier0/wcag.py already covers those same checks via
# axe-core, so surfacing them again here would just double-report the same
# underlying issue under a different pipeline/rule name.
_PROMOTED_AUDITS = (
    "color-contrast",
    "image-alt",
    "label",
    "tap-targets",
    "viewport",
    "largest-contentful-paint",
    "cumulative-layout-shift",
    "total-blocking-time",
    "errors-in-console",
    "no-vulnerable-libraries",
    "uses-https",
)

# Owned by tier0/wcag.py via axe-core -- never emit as a ux finding even if
# Lighthouse reports them as failing.
_AXE_OWNED_AUDITS = frozenset({"image-alt", "label", "color-contrast"})


def _unavailable(reason: str) -> List[Finding]:
    logger.info("lighthouse unavailable: %s", reason)
    return [
        {
            "url": "",
            "pipeline": "ux",
            "tier": 0,
            "rule": "ux/lighthouse-unavailable",
            "severity": "info",
            "title": "Lighthouse unavailable",
            "detail": reason,
            "evidence": {},
        }
    ]


def _find_json_start(text: str) -> Optional[int]:
    idx = text.find("{")
    return idx if idx != -1 else None


def _parse_report(stdout: str) -> Dict[str, Any]:
    """Parse a Lighthouse JSON report out of stdout, which may carry
    leading junk (npx/node banners, warnings) before the first '{'."""
    start = _find_json_start(stdout)
    if start is None:
        raise ValueError("no JSON object found in lighthouse output")
    return json.loads(stdout[start:])


def _category_severity(score: Optional[float]) -> str:
    if score is None:
        return "info"
    if score < config.LIGHTHOUSE_SCORE_SERIOUS:
        return "serious"
    if score < config.LIGHTHOUSE_SCORE_MODERATE:
        return "moderate"
    return "info"


def _category_findings(url: str, report: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    categories = report.get("categories") or {}
    for cat_id in config.LIGHTHOUSE_CATEGORIES:
        cat = categories.get(cat_id)
        if not cat:
            continue
        score = cat.get("score")
        severity = _category_severity(score)
        title = cat.get("title") or cat_id
        findings.append(
            {
                "url": url,
                "pipeline": "ux",
                "tier": 0,
                "rule": f"ux/lighthouse/{cat_id}",
                "severity": severity,
                "title": f"Lighthouse {title} score: {score if score is not None else 'n/a'}",
                "detail": (
                    f"Lighthouse category '{title}' scored "
                    f"{score if score is not None else 'n/a'} (0-1 scale)."
                ),
                "evidence": {"category": cat_id, "score": score},
            }
        )
    return findings


def _audit_failed(audit: Dict[str, Any]) -> bool:
    """An audit "fails" for our purposes when Lighthouse scores it 0, or
    when it's an "error" display mode (Lighthouse couldn't even run the
    audit, itself worth surfacing)."""
    score = audit.get("score")
    if score == 0:
        return True
    if score is None and audit.get("scoreDisplayMode") == "error":
        return True
    return False


def _audit_findings(url: str, report: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    audits = report.get("audits") or {}
    for audit_id in _PROMOTED_AUDITS:
        if audit_id in _AXE_OWNED_AUDITS:
            # De-duped: tier0/wcag.py already reports these via axe-core.
            continue
        audit = audits.get(audit_id)
        if not audit:
            continue
        if not _audit_failed(audit):
            continue
        title = audit.get("title") or audit_id
        description = audit.get("description") or ""
        display_value = audit.get("displayValue")
        detail = description
        if display_value:
            detail = f"{description} ({display_value})".strip()
        findings.append(
            {
                "url": url,
                "pipeline": "ux",
                "tier": 0,
                "rule": f"ux/audit/{audit_id}",
                "severity": "serious",
                "title": title,
                "detail": detail,
                "evidence": {
                    "audit_id": audit_id,
                    "score": audit.get("score"),
                    "scoreDisplayMode": audit.get("scoreDisplayMode"),
                    "displayValue": display_value,
                },
            }
        )
    return findings


def run_lighthouse(url: str) -> List[Finding]:
    """Run Lighthouse against `url` via subprocess and return canonical
    findings. Never raises: any failure (missing node/npx/lighthouse,
    timeout, unparseable output) degrades to a single info finding."""

    only_categories = ",".join(config.LIGHTHOUSE_CATEGORIES)
    cmd = [
        "npx",
        "--no-install",
        "lighthouse",
        url,
        "--output=json",
        "--output-path=stdout",
        "--quiet",
        f"--only-categories={only_categories}",
        '--chrome-flags="--headless=new --no-sandbox --disable-gpu"',
    ]

    env = dict(os.environ)
    chrome_path = config.chrome_path()
    if chrome_path:
        env["CHROME_PATH"] = chrome_path

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.LIGHTHOUSE_TIMEOUT_SECS,
            env=env,
        )
    except FileNotFoundError as exc:
        result = _unavailable(f"lighthouse/npx executable not found: {exc}")
        result[0]["url"] = url
        return result
    except subprocess.TimeoutExpired as exc:
        result = _unavailable(
            f"lighthouse timed out after {config.LIGHTHOUSE_TIMEOUT_SECS}s: {exc}"
        )
        result[0]["url"] = url
        return result
    except subprocess.CalledProcessError as exc:
        result = _unavailable(f"lighthouse process failed: {exc}")
        result[0]["url"] = url
        return result

    try:
        report = _parse_report(proc.stdout or "")
    except (ValueError, json.JSONDecodeError) as exc:
        reason = (
            f"could not parse lighthouse output (rc={proc.returncode}): {exc}. "
            f"stderr: {(proc.stderr or '')[:500]}"
        )
        result = _unavailable(reason)
        result[0]["url"] = url
        return result

    if proc.returncode != 0 and not report.get("categories"):
        reason = (
            f"lighthouse exited with code {proc.returncode} and no usable "
            f"report. stderr: {(proc.stderr or '')[:500]}"
        )
        result = _unavailable(reason)
        result[0]["url"] = url
        return result

    try:
        findings: List[Finding] = []
        findings.extend(_category_findings(url, report))
        findings.extend(_audit_findings(url, report))
        return findings
    except Exception as exc:  # defensive: never raise out of run_lighthouse
        result = _unavailable(f"error interpreting lighthouse report: {exc}")
        result[0]["url"] = url
        return result
