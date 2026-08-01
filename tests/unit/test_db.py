import pytest

import config
import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point config.DB_PATH at a throwaway file per test and force db.py to
    reconnect against it."""
    db.reset()
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "qa.db"))
    db.init_db()
    yield
    db.reset()


def test_init_db_is_idempotent():
    # Called again in the same test (fixture already called it once on setup).
    db.init_db()
    db.init_db()
    # Schema should still work fine afterwards.
    run_id = db.create_run("scan", "https://example.com", {"foo": "bar"})
    assert db.run_summary(run_id) is not None


def test_create_run_and_run_summary():
    spec = {"pages": ["/", "/about"], "depth": 2}
    run_id = db.create_run("full", "https://example.com", spec)

    assert isinstance(run_id, str)
    assert len(run_id) == 32  # uuid4().hex

    summary = db.run_summary(run_id)
    assert summary is not None
    assert summary["id"] == run_id
    assert summary["kind"] == "full"
    assert summary["target"] == "https://example.com"
    assert summary["spec"] == spec
    assert summary["status"] == "running"
    assert summary["finished_at"] is None
    assert summary["stats"] is None
    assert summary["error"] is None
    assert summary["started_at"] is not None


def test_run_summary_missing_returns_none():
    assert db.run_summary("does-not-exist") is None


def test_finish_run_success_sets_stats():
    run_id = db.create_run("scan", "https://example.com", {})
    db.finish_run(run_id, "done", stats={"pages": 5, "findings": 3})

    summary = db.run_summary(run_id)
    assert summary["status"] == "done"
    assert summary["stats"] == {"pages": 5, "findings": 3}
    assert summary["error"] is None
    assert summary["finished_at"] is not None


def test_finish_run_failure_sets_error():
    run_id = db.create_run("scan", "https://example.com", {})
    db.finish_run(run_id, "failed", error="boom")

    summary = db.run_summary(run_id)
    assert summary["status"] == "failed"
    assert summary["error"] == "boom"
    assert summary["stats"] is None


def test_add_finding_and_run_findings_round_trip():
    run_id = db.create_run("scan", "https://example.com", {})
    finding = {
        "url": "https://example.com/page",
        "pipeline": "security",
        "tier": 1,
        "rule": "missing-csp",
        "severity": "serious",
        "title": "Missing Content-Security-Policy header",
        "detail": "No CSP header was found on the response.",
        "evidence": {"headers": {"content-type": "text/html"}, "status": 200},
    }
    finding_id = db.add_finding(run_id, finding)
    assert isinstance(finding_id, int)

    findings = db.run_findings(run_id)
    assert len(findings) == 1
    got = findings[0]
    assert got["id"] == finding_id
    assert got["run_id"] == run_id
    assert got["url"] == finding["url"]
    assert got["pipeline"] == "security"
    assert got["tier"] == 1
    assert got["rule"] == "missing-csp"
    assert got["severity"] == "serious"
    assert got["title"] == finding["title"]
    assert got["detail"] == finding["detail"]
    assert got["evidence"] == finding["evidence"]
    assert got["created_at"] is not None


def test_add_finding_ignores_extra_keys():
    run_id = db.create_run("scan", "https://example.com", {})
    finding = {
        "url": "https://example.com/",
        "pipeline": "wcag",
        "tier": 0,
        "rule": "alt-text",
        "severity": "moderate",
        "title": "Missing alt text",
        "detail": "img missing alt",
        "evidence": {"selector": "img#logo"},
        "unexpected_extra_field": "should be ignored, not crash",
        "another_bogus_key": {"nested": True},
    }
    finding_id = db.add_finding(run_id, finding)
    assert isinstance(finding_id, int)

    findings = db.run_findings(run_id)
    assert len(findings) == 1
    assert "unexpected_extra_field" not in findings[0]


def test_run_findings_multiple_ordered_and_scoped_to_run():
    run_a = db.create_run("scan", "https://a.example.com", {})
    run_b = db.create_run("scan", "https://b.example.com", {})

    base_finding = {
        "url": "https://a.example.com/",
        "pipeline": "ux",
        "tier": 2,
        "rule": "slow-lcp",
        "severity": "minor",
        "title": "Slow LCP",
        "detail": "LCP over budget",
        "evidence": {"lcp_ms": 4200},
    }
    id1 = db.add_finding(run_a, base_finding)
    id2 = db.add_finding(run_a, {**base_finding, "rule": "slow-fid"})
    db.add_finding(run_b, {**base_finding, "rule": "unrelated"})

    findings_a = db.run_findings(run_a)
    assert [f["id"] for f in findings_a] == [id1, id2]
    assert all(f["run_id"] == run_a for f in findings_a)

    findings_b = db.run_findings(run_b)
    assert len(findings_b) == 1
    assert findings_b[0]["rule"] == "unrelated"


def test_run_findings_empty_for_unknown_run():
    assert db.run_findings("no-such-run") == []


def test_init_db_drops_legacy_page_cache_table():
    # Simulate a pre-history DB that still carries the removed cache table.
    conn = db._get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS page_cache (cache_key TEXT PRIMARY KEY)")
    conn.commit()
    db.init_db()
    rows = db._get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='page_cache'"
    ).fetchall()
    assert rows == []
