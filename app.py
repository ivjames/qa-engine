"""qa-engine Flask app: the two SSE run endpoints plus the UI and health check.

Concurrency: gunicorn runs gthread workers; each run drives its own asyncio
loop in a worker thread (see pipelines.stream_async). A bounded semaphore caps
simultaneous heavy runs so we don't fork unbounded chromiums."""

import json
import os
import threading

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_from_directory,
                   stream_with_context, url_for)

import config
import db
import reports

app = Flask(__name__)
db.init_db()

app.template_filter("ts")(reports.fmt_ts)
app.template_filter("pathof")(reports.path_of)
app.template_filter("evsum")(reports.evidence_summary)
app.template_filter("prettyjson")(reports.pretty_json)
app.jinja_env.globals["SEVERITIES"] = reports.SEVERITIES

_run_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_RUNS)
_SCREENSHOT_DIR = os.path.join(os.path.dirname(config.DB_PATH), "screenshots")

_SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
    "Connection": "keep-alive",
}


def _sse_response(frame_gen):
    """Wrap a frame generator: acquire a run slot, release it when the client
    finishes or disconnects. 503 if all slots are busy."""
    if not _run_slots.acquire(blocking=False):
        return Response(
            'event: error\ndata: {"scope":"fatal","message":"server busy — '
            'too many concurrent runs"}\n\n',
            status=503, headers=_SSE_HEADERS)

    @stream_with_context
    def guarded():
        try:
            yield from frame_gen
        finally:
            _run_slots.release()

    return Response(guarded(), headers=_SSE_HEADERS)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "mock_models": config.MOCK_MODELS})


@app.post("/api/run/page")
def run_page():
    from pipelines.page import sse_page
    body = request.get_json(silent=True) or {}
    url = body.get("url")
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    spec = body.get("spec") or {}
    return _sse_response(sse_page(url, spec))


@app.post("/api/run/flow")
def run_flow():
    from pipelines.flow import sse_flow
    body = request.get_json(silent=True) or {}
    flow = body.get("flow")
    if not flow or not isinstance(flow, dict):
        return jsonify({"error": "missing 'flow' object"}), 400
    return _sse_response(sse_flow(flow))


@app.post("/api/run/repo")
def run_repo():
    from pipelines.repo import sse_repo
    body = request.get_json(silent=True) or {}
    repo = (body.get("repo") or "").strip()
    if not repo:
        return jsonify({"error": "missing 'repo'"}), 400
    branch = (body.get("branch") or "main").strip() or "main"
    base = (body.get("base") or "").strip() or None
    spec = body.get("spec") or {}
    return _sse_response(sse_repo(repo, branch, base, spec))


# ---------------------------------------------------------------------
# run history
# ---------------------------------------------------------------------

def _pages_for(summary, findings=None, distinct_urls=None):
    """stats.pages when the run recorded it, else the distinct-URL count."""
    stats = summary.get("stats") or {}
    pages = stats.get("pages")
    if isinstance(pages, (int, float)) and not isinstance(pages, bool):
        return int(pages)
    if distinct_urls is not None:
        return distinct_urls
    return len({f.get("url") for f in (findings or [])})


@app.get("/runs")
def runs_index():
    runs = db.list_runs()
    for r in runs:
        r["pages"] = _pages_for(r, distinct_urls=r["distinct_urls"])
    return render_template(
        "runs.html", runs=runs, error=request.args.get("error"))


@app.post("/runs/import")
def runs_import():
    raw = ""
    f = request.files.get("file")
    if f and f.filename:
        raw = f.read().decode("utf-8", errors="replace")
    if not raw.strip():
        raw = request.form.get("json") or ""
    if not raw.strip():
        return redirect(url_for(
            "runs_index", error="Choose a file or paste the export JSON."))
    try:
        parsed = reports.parse_export(raw)
    except ValueError as exc:
        return redirect(url_for("runs_index", error=str(exc)))

    # Reconstruct the run row's timeline from what the export carries:
    # finished ≈ exported_at, started ≈ finished − duration.
    finished_at = parsed["exported_at"]
    duration = (parsed["stats"] or {}).get("duration_secs")
    started_at = None
    if finished_at is not None:
        started_at = finished_at
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            started_at = finished_at - duration
    created = db.replace_run(
        {
            "id": parsed["run_id"],
            "kind": parsed["kind"],
            "target": parsed["target"],
            "spec": None,
            "status": parsed["status"],
            "started_at": started_at,
            "finished_at": finished_at,
            "stats": parsed["stats"] or None,
            "error": None,
        },
        parsed["findings"],
    )
    return redirect(url_for(
        "run_detail", run_id=parsed["run_id"],
        saved="new" if created else "updated"))


@app.get("/runs/<run_id>")
def run_detail(run_id):
    summary = db.run_summary(run_id)
    if summary is None:
        abort(404)
    findings = db.run_findings(run_id)
    groups = reports.group_findings(findings)
    for g in groups:
        g["raw_json"] = reports.pretty_json([
            {"url": o.get("url"), "severity": o.get("severity"),
             "title": o.get("title"), "evidence": o.get("evidence")}
            for o in g["occurrences"]
        ])
    return render_template(
        "run_detail.html",
        run=summary,
        groups=groups,
        counts=reports.counts_by_severity(findings),
        pages=_pages_for(summary, findings=findings),
        saved=request.args.get("saved"),
    )


@app.post("/runs/<run_id>/delete")
def run_delete(run_id):
    db.delete_run(run_id)
    return redirect(url_for("runs_index"))


@app.get("/runs/<run_id>/export.json")
def run_export(run_id):
    summary = db.run_summary(run_id)
    if summary is None:
        abort(404)
    payload = reports.run_export_dict(summary, db.run_findings(run_id))
    name = "qa-" + (summary.get("kind") or "run") + "-" + run_id[:12] + ".json"
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="' + name + '"'})


@app.get("/screenshots/<run_id>/<path:filename>")
def screenshots(run_id, filename):
    directory = os.path.join(_SCREENSHOT_DIR, run_id)
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=config.PORT, threaded=True)
