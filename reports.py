"""Run-history report logic: export parsing, issue grouping, display helpers.

Pure functions only (no DB, no Flask) so everything here is unit-testable.
Ported from artificial-atheist's lib/qaReports.ts, which consumed this app's
browser "Export JSON" — the parser accepts exactly that shape, extended with
qa-engine's native "critical" severity tier.
"""

import json
from datetime import datetime, timezone

# Index = rank: lower is worse. Matches the UI's SEV_ORDER.
SEVERITIES = ["critical", "serious", "moderate", "minor", "info"]

_SEVERITY_SET = set(SEVERITIES)


def severity_rank(severity):
    """Unknown severities sort as most severe so a new tier is never buried."""
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return -1


def coerce_severity(severity):
    """Unknown/missing severity becomes "info" — findings are never dropped."""
    return severity if severity in _SEVERITY_SET else "info"


def counts_by_severity(findings):
    counts = {sev: 0 for sev in SEVERITIES}
    for f in findings:
        counts[coerce_severity(f.get("severity"))] += 1
    return counts


def _parse_exported_at(value):
    """Lenient ISO-8601 → epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _coerce_str(value, default=""):
    return default if value is None else str(value)


def parse_export(raw):
    """Validate + normalize a QA export JSON string.

    Raises ValueError with a user-facing message on anything that isn't a
    plausible export. Every finding is coerced (never dropped): missing
    pipeline/rule become "unknown", unknown severity becomes "info", title
    falls back to the rule name.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Not valid JSON.")
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object at the top level.")
    if not isinstance(data.get("findings"), list):
        raise ValueError('Missing "findings" array — is this a QA export file?')
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('Missing "run_id" — is this a QA export file?')

    findings = []
    for f in data["findings"]:
        if not isinstance(f, dict):
            f = {}
        rule = _coerce_str(f.get("rule"), "unknown") or "unknown"
        tier = f.get("tier")
        findings.append({
            "url": _coerce_str(f.get("url")),
            "pipeline": _coerce_str(f.get("pipeline"), "unknown") or "unknown",
            "tier": tier if isinstance(tier, int) else None,
            "rule": rule,
            "severity": coerce_severity(f.get("severity")),
            "title": _coerce_str(f.get("title")) or rule or "Untitled finding",
            "detail": _coerce_str(f.get("detail")),
            "evidence": f.get("evidence"),
        })

    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    pages = stats.get("pages")
    if not isinstance(pages, (int, float)) or isinstance(pages, bool):
        pages = len({f["url"] for f in findings})

    return {
        "run_id": run_id,
        "kind": _coerce_str(data.get("kind"), "page") or "page",
        "target": _coerce_str(data.get("target")),
        "status": _coerce_str(data.get("status")),
        "exported_at": _parse_exported_at(data.get("exported_at")),
        "pages": int(pages),
        "stats": stats,
        "findings": findings,
        "counts": counts_by_severity(findings),
    }


def group_findings(findings):
    """Collapse flat per-page findings into distinct issues.

    Grouped on (pipeline, rule) — the same rule name in different pipelines
    stays separate. Each group wears the severity/title/detail of its WORST
    occurrence (first seen wins ties), keeping every occurrence for the
    per-page breakdown. Sorted worst severity first, then most occurrences,
    then rule name.
    """
    groups = {}
    for f in findings:
        key = (f.get("pipeline"), f.get("rule"))
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "pipeline": f.get("pipeline"),
                "rule": f.get("rule"),
                "severity": f.get("severity"),
                "title": f.get("title"),
                "detail": f.get("detail"),
                "occurrences": [f],
            }
            continue
        g["occurrences"].append(f)
        if severity_rank(f.get("severity")) < severity_rank(g["severity"]):
            g["severity"] = f.get("severity")
            g["title"] = f.get("title")
            g["detail"] = f.get("detail")
    return sorted(
        groups.values(),
        key=lambda g: (
            severity_rank(g["severity"]),
            -len(g["occurrences"]),
            g["rule"] or "",
        ),
    )


def run_export_dict(summary, findings):
    """Server-side equivalent of the browser's "Export JSON" for a stored run.

    Same shape, built from the DB. Errors are streamed but never persisted,
    so the errors list is always empty here.
    """
    return {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "kind": summary.get("kind"),
        "target": summary.get("target"),
        "run_id": summary.get("id"),
        "status": summary.get("status"),
        "stats": summary.get("stats"),
        "findings": [
            {
                "type": "finding",
                "id": f.get("id"),
                "url": f.get("url"),
                "pipeline": f.get("pipeline"),
                "tier": f.get("tier"),
                "rule": f.get("rule"),
                "severity": f.get("severity"),
                "title": f.get("title"),
                "detail": f.get("detail"),
                "evidence": f.get("evidence"),
            }
            for f in findings
        ],
        "errors": [],
    }


# ---------------------------------------------------------------------
# display helpers (used by the /runs templates)
# ---------------------------------------------------------------------

def fmt_ts(epoch):
    """Epoch seconds → "YYYY-MM-DD HH:MM UTC", or "—" for None."""
    if not isinstance(epoch, (int, float)):
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def path_of(url):
    """Display a URL as path+query; fall back to the raw string."""
    s = _coerce_str(url)
    marker = "://"
    idx = s.find(marker)
    if idx == -1:
        return s or "/"
    slash = s.find("/", idx + len(marker))
    return s[slash:] if slash != -1 else "/"


def evidence_summary(evidence):
    """One-line inline summary of a finding's evidence blob, or ""."""
    if not isinstance(evidence, dict):
        return ""
    dv = evidence.get("displayValue")
    if isinstance(dv, str) and dv:
        return dv
    score = evidence.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return f"score {score}"
    value = evidence.get("value")
    if isinstance(value, str) and value:
        return value
    node_count = evidence.get("node_count")
    if isinstance(node_count, (int, float)) and not isinstance(node_count, bool):
        return f"{int(node_count)} element(s)"
    return ""


def pretty_json(value):
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
