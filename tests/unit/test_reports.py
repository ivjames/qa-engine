"""Tests for reports.py (export parsing + issue grouping) and the db.py
run-history helpers. The parse/group cases are ported from the consumer that
originally archived these exports (artificial-atheist's qa-reports tests), so
the two ends agree on the format."""

import json

import pytest

import config
import db
import reports


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.reset()
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "qa.db"))
    db.init_db()
    yield
    db.reset()


def finding(**over):
    base = {
        "url": "https://example.com/",
        "pipeline": "security",
        "tier": 0,
        "rule": "missing-csp",
        "severity": "serious",
        "title": "Missing CSP",
        "detail": "No CSP header.",
        "evidence": {"present": False},
    }
    base.update(over)
    return base


VALID_EXPORT = {
    "exported_at": "2026-08-01T01:44:56.202Z",
    "kind": "page",
    "target": "https://example.com",
    "run_id": "abc123def456",
    "status": "done",
    "stats": {"pages": 3, "duration_secs": 42},
    "findings": [
        finding(),
        finding(url="https://example.com/a/", pipeline="wcag",
                rule="heading-order", severity="moderate",
                title="Heading order"),
        finding(url="https://example.com/b/", pipeline="ux", rule="lh-perf",
                severity="info", title="Perf score",
                evidence={"score": 0.9}),
    ],
    "errors": [],
}


# ---------------------------------------------------------------------
# parse_export
# ---------------------------------------------------------------------

def test_parse_valid_export():
    parsed = reports.parse_export(json.dumps(VALID_EXPORT))
    assert parsed["run_id"] == "abc123def456"
    assert parsed["kind"] == "page"
    assert parsed["target"] == "https://example.com"
    assert parsed["status"] == "done"
    assert parsed["pages"] == 3
    assert parsed["exported_at"] is not None
    assert len(parsed["findings"]) == 3
    assert parsed["counts"] == {
        "critical": 0, "serious": 1, "moderate": 1, "minor": 0, "info": 1}


def test_parse_rejects_bad_input():
    with pytest.raises(ValueError, match="valid JSON"):
        reports.parse_export("not json {")
    with pytest.raises(ValueError, match="object"):
        reports.parse_export('"a string"')
    with pytest.raises(ValueError, match="findings"):
        reports.parse_export(json.dumps({"run_id": "x"}))
    with pytest.raises(ValueError, match="run_id"):
        reports.parse_export(json.dumps({"findings": []}))


def test_parse_pages_falls_back_to_distinct_urls():
    export = dict(VALID_EXPORT, stats={})
    export["findings"] = [
        finding(url="https://example.com/a/"),
        finding(url="https://example.com/a/", rule="other"),
        finding(url="https://example.com/b/"),
    ]
    parsed = reports.parse_export(json.dumps(export))
    assert parsed["pages"] == 2


def test_parse_coerces_unknown_severity_to_info():
    export = dict(VALID_EXPORT)
    export["findings"] = [finding(severity="catastrophic")]
    parsed = reports.parse_export(json.dumps(export))
    assert parsed["findings"][0]["severity"] == "info"
    assert parsed["counts"]["info"] == 1


def test_parse_coerces_missing_finding_fields():
    export = dict(VALID_EXPORT)
    export["findings"] = [{"rule": "lonely-rule"}]
    parsed = reports.parse_export(json.dumps(export))
    f = parsed["findings"][0]
    assert f["url"] == ""
    assert f["pipeline"] == "unknown"
    assert f["title"] == "lonely-rule"  # title falls back to the rule
    assert f["severity"] == "info"
    assert f["tier"] is None


# ---------------------------------------------------------------------
# group_findings / severity_rank
# ---------------------------------------------------------------------

def test_group_same_rule_across_pages():
    groups = reports.group_findings([
        finding(url="https://example.com/a/"),
        finding(url="https://example.com/b/"),
        finding(url="https://example.com/c/"),
    ])
    assert len(groups) == 1
    assert len(groups[0]["occurrences"]) == 3


def test_groups_sort_worst_severity_first():
    groups = reports.group_findings([
        finding(rule="server-version", severity="minor"),
        finding(rule="missing-csp", severity="serious"),
        finding(rule="lh-perf", severity="moderate"),
        finding(rule="broken-auth", severity="critical"),
    ])
    assert [g["rule"] for g in groups] == [
        "broken-auth", "missing-csp", "lh-perf", "server-version"]


def test_group_wears_worst_occurrence_title():
    groups = reports.group_findings([
        finding(rule="lh-perf", severity="moderate", title="Perf 0.59"),
        finding(rule="lh-perf", severity="serious", title="Perf 0.39"),
    ])
    assert len(groups) == 1
    assert groups[0]["severity"] == "serious"
    assert groups[0]["title"] == "Perf 0.39"
    assert len(groups[0]["occurrences"]) == 2


def test_same_rule_different_pipelines_not_merged():
    groups = reports.group_findings([
        finding(pipeline="security", rule="same"),
        finding(pipeline="wcag", rule="same"),
    ])
    assert len(groups) == 2


def test_severity_rank_order():
    ranks = [reports.severity_rank(s) for s in
             ["critical", "serious", "moderate", "minor", "info"]]
    assert ranks == sorted(ranks)
    # Unknown tiers sort as most severe so they are never buried.
    assert reports.severity_rank("brand-new-tier") < reports.severity_rank("critical")


# ---------------------------------------------------------------------
# display helpers
# ---------------------------------------------------------------------

def test_evidence_summary_priority():
    assert reports.evidence_summary({"displayValue": "1.2 s"}) == "1.2 s"
    assert reports.evidence_summary({"score": 0.9}) == "score 0.9"
    assert reports.evidence_summary({"value": "nginx/1.18"}) == "nginx/1.18"
    assert reports.evidence_summary({"node_count": 4}) == "4 element(s)"
    assert reports.evidence_summary({"other": True}) == ""
    assert reports.evidence_summary(None) == ""


def test_path_of():
    assert reports.path_of("https://example.com/a/b?q=1") == "/a/b?q=1"
    assert reports.path_of("https://example.com") == "/"
    assert reports.path_of("not a url") == "not a url"


# ---------------------------------------------------------------------
# db helpers: list_runs / delete_run / replace_run
# ---------------------------------------------------------------------

def _seed_run(target, findings=(), stats=None):
    run_id = db.create_run("page", target, {})
    for f in findings:
        db.add_finding(run_id, f)
    db.finish_run(run_id, "done", stats=stats)
    return run_id


def test_list_runs_newest_first_with_counts():
    a = _seed_run("https://a.example.com",
                  [finding(), finding(severity="info", rule="lh-perf")],
                  stats={"pages": 5})
    b = _seed_run("https://b.example.com")

    runs = db.list_runs()
    assert [r["id"] for r in runs] == [b, a]
    by_id = {r["id"]: r for r in runs}
    assert by_id[a]["severity_counts"] == {"serious": 1, "info": 1}
    assert by_id[a]["distinct_urls"] == 1
    assert by_id[a]["stats"] == {"pages": 5}
    assert by_id[b]["severity_counts"] == {}


def test_delete_run_removes_findings_too():
    run_id = _seed_run("https://a.example.com", [finding()])
    assert db.delete_run(run_id) is True
    assert db.run_summary(run_id) is None
    assert db.run_findings(run_id) == []
    assert db.delete_run(run_id) is False


def test_replace_run_import_and_refresh():
    parsed = reports.parse_export(json.dumps(VALID_EXPORT))
    summary = {
        "id": parsed["run_id"], "kind": parsed["kind"],
        "target": parsed["target"], "spec": None, "status": parsed["status"],
        "started_at": None, "finished_at": parsed["exported_at"],
        "stats": parsed["stats"], "error": None,
    }
    created = db.replace_run(summary, parsed["findings"])
    assert created is True
    assert len(db.run_findings(parsed["run_id"])) == 3

    # Re-import of the same run id refreshes instead of duplicating.
    created = db.replace_run(summary, parsed["findings"][:1])
    assert created is False
    assert len(db.run_findings(parsed["run_id"])) == 1
    got = db.run_summary(parsed["run_id"])
    assert got["target"] == "https://example.com"
    assert got["stats"] == {"pages": 3, "duration_secs": 42}


def test_replace_run_round_trips_export_shape():
    parsed = reports.parse_export(json.dumps(VALID_EXPORT))
    summary = {
        "id": parsed["run_id"], "kind": parsed["kind"],
        "target": parsed["target"], "spec": None, "status": parsed["status"],
        "started_at": None, "finished_at": parsed["exported_at"],
        "stats": parsed["stats"], "error": None,
    }
    db.replace_run(summary, parsed["findings"])
    export = reports.run_export_dict(
        db.run_summary(parsed["run_id"]), db.run_findings(parsed["run_id"]))
    assert export["run_id"] == parsed["run_id"]
    assert export["stats"] == parsed["stats"]
    assert len(export["findings"]) == 3
    # The server-side export must itself re-parse cleanly (AA can ingest it).
    reparsed = reports.parse_export(json.dumps(export))
    assert reparsed["counts"] == parsed["counts"]
