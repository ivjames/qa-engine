"""End-to-end page-pipeline smoke test (keyless / MOCK_MODELS).

Serves tests/fixtures over http.server, runs the SSE page pipeline against
sample.html, and asserts: crawl+template dedup happened, Tier-0 findings were
produced (axe + security), the SSE frame order is sane, and a SECOND run over
the same content is a cache hit (no re-review). Run:

    MOCK_MODELS=1 .venv/bin/python -m tests.smoke_page
"""

import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MOCK_MODELS", "1")

import config  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _serve():
    handler = partial(SimpleHTTPRequestHandler, directory=FIXTURES)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _collect(frame_gen):
    events = []
    for frame in frame_gen:
        if frame.startswith(":"):
            continue
        etype = data = None
        for line in frame.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if etype:
            events.append((etype, json.loads(data) if data else {}))
    return events


def main():
    # fresh db per smoke run
    config.DB_PATH = os.path.join(FIXTURES, "_smoke.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(config.DB_PATH + suffix)
        except FileNotFoundError:
            pass

    import db
    db.reset()
    db.init_db()

    from pipelines.page import sse_page

    httpd, port = _serve()
    try:
        seed = f"http://127.0.0.1:{port}/sample.html"
        spec = {"ux": False}  # skip Lighthouse to keep the smoke fast

        events = _collect(sse_page(seed, spec))
        types = [e[0] for e in events]
        findings = [e[1] for e in events if e[0] == "finding"]

        assert types[0] == "run_start", types[:3]
        assert types[-1] == "done", types[-3:]
        assert "stage" in types
        assert "page_progress" in types

        rules = {f["rule"] for f in findings}
        pipelines = {f["pipeline"] for f in findings}
        assert findings, "expected Tier-0 findings"
        assert "security" in pipelines, f"no security findings: {pipelines}"
        assert "wcag" in pipelines, f"no wcag findings: {pipelines}"
        # planted secret + missing headers
        assert "aws-access-key" in rules, f"secret not found: {rules}"
        assert "missing-csp" in rules, f"header check missing: {rules}"

        done = events[-1][1]["stats"]
        assert done["templates"] >= 1
        assert done["pages"] >= 1
        first_findings = len(findings)

        # SECOND run: identical content -> cache hit, findings replayed, no
        # fresh review. cache_hits must be > 0.
        events2 = _collect(sse_page(seed, spec))
        done2 = events2[-1][1]["stats"]
        assert done2["cache_hits"] >= 1, f"expected cache hit, got {done2}"

        print(f"OK  frames={len(events)}  findings={first_findings}  "
              f"templates={done['templates']} pages={done['pages']} "
              f"pipelines={sorted(pipelines)}  cache_hits(2nd run)={done2['cache_hits']}")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
