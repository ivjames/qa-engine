"""Unit tests for tier0.ux -- the Lighthouse-driven Tier-0 UX pipeline.

No live lighthouse/node/chrome required: subprocess.run is monkeypatched
so these tests are deterministic and run in any container, including ones
where lighthouse isn't actually installed.
"""

import json
import subprocess

import pytest

from tier0 import ux


URL = "https://example.com/"


def rule_set(findings):
    return {f["rule"] for f in findings}


def find_by_rule(findings, rule):
    return [f for f in findings if f["rule"] == rule]


def assert_canonical_shape(finding):
    assert finding["pipeline"] == "ux"
    assert finding["tier"] == 0
    assert set(
        ["url", "pipeline", "tier", "rule", "severity", "title", "detail", "evidence"]
    ) <= set(finding.keys())
    assert finding["severity"] in {"info", "minor", "moderate", "serious", "critical"}
    assert isinstance(finding["evidence"], dict)


class _CompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------

def test_missing_node_or_npx_returns_single_info_finding(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("npx not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    findings = ux.run_lighthouse(URL)

    assert len(findings) == 1
    f = findings[0]
    assert_canonical_shape(f)
    assert f["rule"] == "ux/lighthouse-unavailable"
    assert f["severity"] == "info"
    assert f["url"] == URL


def test_timeout_returns_single_info_finding(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lighthouse", timeout=90)

    monkeypatch.setattr(subprocess, "run", fake_run)

    findings = ux.run_lighthouse(URL)

    assert len(findings) == 1
    assert findings[0]["rule"] == "ux/lighthouse-unavailable"
    assert findings[0]["severity"] == "info"


def test_unparseable_stdout_returns_single_info_finding(monkeypatch):
    def fake_run(*args, **kwargs):
        return _CompletedProcess(stdout="not json at all", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    findings = ux.run_lighthouse(URL)

    assert len(findings) == 1
    assert findings[0]["rule"] == "ux/lighthouse-unavailable"
    assert findings[0]["severity"] == "info"


def test_nonzero_exit_with_no_report_returns_single_info_finding(monkeypatch):
    def fake_run(*args, **kwargs):
        return _CompletedProcess(stdout="", stderr="chrome crashed", returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    findings = ux.run_lighthouse(URL)

    assert len(findings) == 1
    assert findings[0]["rule"] == "ux/lighthouse-unavailable"
    assert findings[0]["severity"] == "info"


def test_never_raises_on_arbitrary_exception(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "lighthouse")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Must not raise -- CalledProcessError isn't explicitly caught in the
    # implementation's narrow except clauses around subprocess.run, so this
    # also guards against a regression that lets it escape.
    try:
        findings = ux.run_lighthouse(URL)
    except Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"run_lighthouse raised: {exc!r}")

    assert len(findings) == 1
    assert findings[0]["rule"] == "ux/lighthouse-unavailable"


# ---------------------------------------------------------------------------
# canned report: categories + promoted audits + axe-owned suppression
# ---------------------------------------------------------------------------

CANNED_REPORT = {
    "categories": {
        "performance": {"title": "Performance", "score": 0.3},
        "accessibility": {"title": "Accessibility", "score": 0.95},
        "best-practices": {"title": "Best Practices", "score": 0.99},
    },
    "audits": {
        "total-blocking-time": {
            "title": "Total Blocking Time",
            "description": "Sum of all time periods.",
            "score": 0,
            "scoreDisplayMode": "numeric",
            "displayValue": "890 ms",
        },
        "viewport": {
            "title": "Has a `<meta name=\"viewport\">` tag",
            "description": "A viewport meta tag optimizes for mobile.",
            "score": 0,
            "scoreDisplayMode": "binary",
        },
        # Must be suppressed even though it scores 0 -- owned by tier0/wcag.py.
        "image-alt": {
            "title": "Image elements have [alt] attributes",
            "description": "Informative elements should aim for short alt text.",
            "score": 0,
            "scoreDisplayMode": "binary",
        },
        "label": {
            "title": "Form elements have associated labels",
            "description": "Labels ensure form controls are announced.",
            "score": 0,
            "scoreDisplayMode": "binary",
        },
        "color-contrast": {
            "title": "Background/foreground colors have sufficient contrast",
            "description": "Low-contrast text is difficult to read.",
            "score": 0,
            "scoreDisplayMode": "binary",
        },
        # Passing audit -- must not appear.
        "uses-https": {
            "title": "Uses HTTPS",
            "description": "All sites should be protected with HTTPS.",
            "score": 1,
            "scoreDisplayMode": "binary",
        },
    },
}


def _fake_run_with_canned_report(monkeypatch, report=CANNED_REPORT, returncode=0):
    def fake_run(cmd, capture_output, text, timeout, env):
        # Sanity: env carries CHROME_PATH through when config.chrome_path()
        # returns something, and always carries the ambient env through.
        assert "PATH" in env
        stdout = "some npx banner junk\n" + json.dumps(report)
        return _CompletedProcess(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_category_and_audit_findings_with_axe_owned_suppression(monkeypatch):
    _fake_run_with_canned_report(monkeypatch)

    findings = ux.run_lighthouse(URL)

    assert findings, "expected findings from canned report"
    for f in findings:
        assert_canonical_shape(f)
        assert f["url"] == URL

    rules = rule_set(findings)

    # Category findings.
    assert "ux/lighthouse/performance" in rules
    perf = find_by_rule(findings, "ux/lighthouse/performance")[0]
    assert perf["severity"] == "serious"  # 0.3 < LIGHTHOUSE_SCORE_SERIOUS (0.5)

    assert "ux/lighthouse/accessibility" in rules
    a11y = find_by_rule(findings, "ux/lighthouse/accessibility")[0]
    assert a11y["severity"] == "info"  # 0.95 >= LIGHTHOUSE_SCORE_MODERATE (0.9)

    # Promoted audit findings for failing audits.
    assert "ux/audit/total-blocking-time" in rules
    tbt = find_by_rule(findings, "ux/audit/total-blocking-time")[0]
    assert tbt["severity"] == "serious"
    assert tbt["evidence"]["displayValue"] == "890 ms"

    assert "ux/audit/viewport" in rules
    viewport = find_by_rule(findings, "ux/audit/viewport")[0]
    assert viewport["severity"] == "serious"

    # Axe-owned audits must NOT be emitted as ux findings, even though they
    # score 0 in the canned report -- tier0/wcag.py owns these via axe-core.
    assert "ux/audit/image-alt" not in rules
    assert "ux/audit/label" not in rules
    assert "ux/audit/color-contrast" not in rules

    # A passing audit (uses-https, score 1) must not produce a finding.
    assert "ux/audit/uses-https" not in rules


def test_high_scoring_categories_are_info(monkeypatch):
    report = {
        "categories": {
            "performance": {"title": "Performance", "score": 0.98},
            "accessibility": {"title": "Accessibility", "score": 1.0},
            "best-practices": {"title": "Best Practices", "score": 1.0},
        },
        "audits": {},
    }
    _fake_run_with_canned_report(monkeypatch, report=report)

    findings = ux.run_lighthouse(URL)

    for cat in ("performance", "accessibility", "best-practices"):
        matches = find_by_rule(findings, f"ux/lighthouse/{cat}")
        assert len(matches) == 1
        assert matches[0]["severity"] == "info"


def test_null_category_score_is_info(monkeypatch):
    report = {
        "categories": {
            "performance": {"title": "Performance", "score": None},
        },
        "audits": {},
    }
    _fake_run_with_canned_report(monkeypatch, report=report)

    findings = ux.run_lighthouse(URL)

    matches = find_by_rule(findings, "ux/lighthouse/performance")
    assert len(matches) == 1
    assert matches[0]["severity"] == "info"


def test_never_raises_with_malformed_report_shape(monkeypatch):
    # "categories"/"audits" present but with unexpected inner shapes.
    report = {"categories": {"performance": "not-a-dict"}, "audits": {"viewport": None}}
    _fake_run_with_canned_report(monkeypatch, report=report)

    try:
        findings = ux.run_lighthouse(URL)
    except Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"run_lighthouse raised on malformed report: {exc!r}")

    # Whatever happens, we must get back a list without raising.
    assert isinstance(findings, list)
