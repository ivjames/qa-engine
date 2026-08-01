"""qa-engine SQLite persistence layer. stdlib sqlite3 only.

WAL mode, a single module-level connection shared across gunicorn threads
(check_same_thread=False), guarded by a lock. DB_PATH is re-read from
config at connect time so tests can monkeypatch it before calling reset()
or any of the public functions.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

import config

_lock = threading.RLock()
_conn = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT,
    target TEXT,
    spec TEXT,
    status TEXT,
    started_at REAL,
    finished_at REAL,
    stats TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    url TEXT,
    pipeline TEXT,
    tier INTEGER,
    rule TEXT,
    severity TEXT,
    title TEXT,
    detail TEXT,
    evidence TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_findings_run_id ON findings (run_id);

-- The cross-run page_cache was removed (runs always review fresh so /runs
-- history reflects scan-time reality); drop the dead table from older DBs.
DROP TABLE IF EXISTS page_cache;
"""


def _connect():
    """(Re)open the module-level connection against the current
    config.DB_PATH, creating the parent directory if needed."""
    global _conn
    db_path = config.DB_PATH
    data_dir = os.path.dirname(db_path)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    _conn = conn
    return _conn


def _get_conn():
    with _lock:
        if _conn is None:
            _connect()
        return _conn


def new_run_id():
    """Short unique id for a run, independent of create_run's own id
    allocation. Available for callers (e.g. a pipeline) that want to
    mint an id before the row exists."""
    return uuid.uuid4().hex[:12]


def close():
    """Close the module-level connection, if open. Safe to call multiple
    times. Intended for tests/shutdown; the next call to any public
    function transparently reconnects."""
    reset()


def reset():
    """Test hook: close and drop the module connection so the next call
    re-reads config.DB_PATH and reconnects from scratch."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


def init_db():
    """Idempotent schema creation. Safe to call repeatedly (e.g. on every
    app import)."""
    with _lock:
        conn = _get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()


# ---------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------

def create_run(kind, target, spec, run_id=None):
    if run_id is None:
        run_id = uuid.uuid4().hex
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO runs (id, kind, target, spec, status, started_at, "
            "finished_at, stats, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                kind,
                target,
                json.dumps(spec),
                "running",
                time.time(),
                None,
                None,
                None,
            ),
        )
        conn.commit()
    return run_id


def finish_run(run_id, status, stats=None, error=None):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, stats = ?, error = ? "
            "WHERE id = ?",
            (
                status,
                time.time(),
                json.dumps(stats) if stats is not None else None,
                error,
                run_id,
            ),
        )
        conn.commit()


def run_summary(run_id):
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["spec"] = json.loads(d["spec"]) if d["spec"] is not None else None
    d["stats"] = json.loads(d["stats"]) if d["stats"] is not None else None
    return d


def list_runs(limit=200):
    """Newest-first run summaries for the history page, each annotated with
    per-severity finding counts and the distinct-URL count (the pages
    fallback when stats never landed)."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM runs "
            "ORDER BY COALESCE(finished_at, started_at) DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
        sev_rows = conn.execute(
            "SELECT run_id, severity, COUNT(*) AS n FROM findings "
            "GROUP BY run_id, severity"
        ).fetchall()
        url_rows = conn.execute(
            "SELECT run_id, COUNT(DISTINCT url) AS n FROM findings GROUP BY run_id"
        ).fetchall()
    counts = {}
    for r in sev_rows:
        counts.setdefault(r["run_id"], {})[r["severity"]] = r["n"]
    urls = {r["run_id"]: r["n"] for r in url_rows}
    out = []
    for row in rows:
        d = dict(row)
        d["spec"] = json.loads(d["spec"]) if d["spec"] is not None else None
        d["stats"] = json.loads(d["stats"]) if d["stats"] is not None else None
        d["severity_counts"] = counts.get(d["id"], {})
        d["distinct_urls"] = urls.get(d["id"], 0)
        out.append(d)
    return out


def delete_run(run_id):
    """Delete a run and its findings. Returns True if the run existed."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
        conn.commit()
        return cur.rowcount > 0


def replace_run(summary, findings):
    """Upsert a whole run (the history import path): the run row is replaced
    keyed on id and its findings are swapped wholesale, so re-importing the
    same export refreshes instead of duplicating. Returns True when the run
    id was new, False when an existing run was refreshed."""
    run_id = summary["id"]
    now = time.time()
    with _lock:
        conn = _get_conn()
        existed = (
            conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            is not None
        )
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, kind, target, spec, status, "
            "started_at, finished_at, stats, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                summary.get("kind"),
                summary.get("target"),
                json.dumps(summary["spec"]) if summary.get("spec") is not None else None,
                summary.get("status"),
                summary.get("started_at"),
                summary.get("finished_at"),
                json.dumps(summary["stats"]) if summary.get("stats") is not None else None,
                summary.get("error"),
            ),
        )
        conn.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
        for f in findings:
            conn.execute(
                "INSERT INTO findings (run_id, url, pipeline, tier, rule, severity, "
                "title, detail, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    f.get("url"),
                    f.get("pipeline"),
                    f.get("tier"),
                    f.get("rule"),
                    f.get("severity"),
                    f.get("title"),
                    f.get("detail"),
                    json.dumps(f.get("evidence")),
                    now,
                ),
            )
        conn.commit()
    return not existed


# ---------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------

def add_finding(run_id, finding):
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO findings (run_id, url, pipeline, tier, rule, severity, "
            "title, detail, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                finding.get("url"),
                finding.get("pipeline"),
                finding.get("tier"),
                finding.get("rule"),
                finding.get("severity"),
                finding.get("title"),
                finding.get("detail"),
                json.dumps(finding.get("evidence")),
                time.time(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def run_findings(run_id):
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY id ASC", (run_id,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] is not None else None
        out.append(d)
    return out


# ---------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="qa-engine-db-smoke-")
    config.DB_PATH = os.path.join(tmp_dir, "smoke.db")
    reset()
    init_db()
    init_db()  # idempotent

    run_id = new_run_id()
    assert len(run_id) == 12
    created_run_id = create_run("scan", "https://example.com", {"depth": 2}, run_id=run_id)
    assert created_run_id == run_id

    finding_id = add_finding(run_id, {
        "url": "https://example.com/",
        "pipeline": "wcag",
        "tier": 0,
        "rule": "alt-text",
        "severity": "moderate",
        "title": "Missing alt text",
        "detail": "img missing alt",
        "evidence": {"selector": "img#logo"},
    })
    assert isinstance(finding_id, int)
    assert len(run_findings(run_id)) == 1

    finish_run(run_id, "done", stats={"pages": 1, "findings": 1})
    summary = run_summary(run_id)
    assert summary["status"] == "done"
    assert summary["stats"] == {"pages": 1, "findings": 1}

    close()
    print("OK")
