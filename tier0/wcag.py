"""Tier-0 WCAG pipeline: run the vendored axe-core against a live Playwright
page and map its violations onto the qa-engine canonical finding dict.

No LLM calls here — this is deterministic, tool-driven accessibility
scanning (axe-core), which is why it lives at tier 0.
"""

import os

VENDOR_PATH = os.path.join(os.path.dirname(__file__), "vendor", "axe.min.js")

# axe "impact" -> qa-engine severity
_IMPACT_SEVERITY = {
    "critical": "critical",
    "serious": "serious",
    "moderate": "moderate",
    "minor": "minor",
    None: "info",
}

_SNIPPET_MAX = 200


def _read_vendor_source():
    with open(VENDOR_PATH, "r", encoding="utf-8") as f:
        return f.read()


async def _inject_axe(page):
    """Inject the vendored axe-core build into the page.

    Tries add_script_tag first (fast path); some pages' CSP blocks injected
    <script> tags, so fall back to page.evaluate of the raw source, which
    runs in-page without adding a script element.
    """
    try:
        await page.add_script_tag(path=VENDOR_PATH)
    except Exception:
        source = _read_vendor_source()
        await page.evaluate(source)

    axe_type = await page.evaluate("typeof window.axe")
    return axe_type in ("function", "object")


def _node_selector(node):
    target = node.get("target") or []
    parts = []
    for t in target:
        if isinstance(t, list):
            parts.extend(t)
        else:
            parts.append(t)
    return ", ".join(str(p) for p in parts)


def _node_snippet(node):
    html = node.get("html") or ""
    if len(html) > _SNIPPET_MAX:
        return html[:_SNIPPET_MAX]
    return html


def _map_violation(url, violation):
    impact = violation.get("impact")
    severity = _IMPACT_SEVERITY.get(impact, "info")

    nodes = violation.get("nodes") or []
    evidence_nodes = [
        {"selector": _node_selector(n), "snippet": _node_snippet(n)}
        for n in nodes[:5]
    ]

    tags = violation.get("tags") or []
    help_url = violation.get("helpUrl") or ""
    description = violation.get("description") or ""
    detail = (description + " " + help_url).strip()

    finding = {
        "url": url,
        "pipeline": "wcag",
        "tier": 0,
        "rule": violation.get("id"),
        "severity": severity,
        "title": violation.get("help"),
        "detail": detail,
        "evidence": {
            "impact": impact,
            "nodes": evidence_nodes,
            "node_count": len(nodes),
            "tags": tags,
        },
    }

    # Best-practice-only violations (no wcag2a/wcag2aa tag) are judgment
    # calls, not clear-cut accessibility failures against the standard --
    # flag them for human/LLM review rather than treating them as final.
    is_wcag_tagged = any(t in ("wcag2a", "wcag2aa") for t in tags)
    if not is_wcag_tagged:
        finding["ambiguous"] = True

    return finding


async def run_axe(page, url: str) -> list:
    """Run axe-core against `page` and return a list of canonical finding
    dicts for the given `url`. Returns [] plus a single info-level
    axe-injection-failed finding if axe-core could not be injected/loaded."""

    injected = await _inject_axe(page)
    if not injected:
        return [
            {
                "url": url,
                "pipeline": "wcag",
                "tier": 0,
                "rule": "axe-injection-failed",
                "severity": "info",
                "title": "axe-core could not be injected into the page",
                "detail": (
                    "Both add_script_tag and the evaluate fallback failed to "
                    "make window.axe available; the page may block inline "
                    "script execution entirely."
                ),
                "evidence": {},
            }
        ]

    result = await page.evaluate(
        """
        () => axe.run(document, {
            runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'best-practice']},
            resultTypes: ['violations']
        })
        """
    )

    violations = result.get("violations") or []
    return [_map_violation(url, v) for v in violations]
