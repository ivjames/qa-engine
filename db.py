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

CREATE TABLE IF NOT EXISTS page_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT,
    dom_hash TEXT,
    tier0_json TEXT,
    findings_json TEXT,
    tokens INTEGER,
    created_at REAL
);
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
# page_cache
# ---------------------------------------------------------------------

def cache_get(cache_key):
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM page_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    if row is None:
        return None
    if time.time() - row["created_at"] > config.CACHE_TTL_SECS:
        return None
    return {
        "url": row["url"],
        "dom_hash": row["dom_hash"],
        "tier0": json.loads(row["tier0_json"]) if row["tier0_json"] is not None else None,
        "findings": json.loads(row["findings_json"]) if row["findings_json"] is not None else None,
        "tokens": row["tokens"],
        "created_at": row["created_at"],
    }


def cache_put(cache_key, url, dom_hash, tier0, findings, tokens):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO page_cache (cache_key, url, dom_hash, "
            "tier0_json, findings_json, tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cache_key,
                url,
                dom_hash,
                json.dumps(tier0),
                json.dumps(findings),
                tokens,
                time.time(),
            ),
        )
        conn.commit()


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

    tier0 = {"score": 0.9}
    findings = [{"rule": "contrast", "severity": "moderate"}]
    cache_key = "smoke-key-1"
    cache_put(cache_key, "https://example.com/", "hash-abc", tier0, findings, 128)
    cached = cache_get(cache_key)
    assert cached is not None, "cache_get returned None right after cache_put"
    assert cached["tier0"] == tier0
    assert cached["findings"] == findings
    assert cached["tokens"] == 128

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
