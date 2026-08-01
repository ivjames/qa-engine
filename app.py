"""qa-engine Flask app: the two SSE run endpoints plus the UI and health check.

Concurrency: gunicorn runs gthread workers; each run drives its own asyncio
loop in a worker thread (see pipelines.stream_async). A bounded semaphore caps
simultaneous heavy runs so we don't fork unbounded chromiums."""

import os
import threading

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, stream_with_context)

import config
import db

app = Flask(__name__)
db.init_db()

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


@app.get("/screenshots/<run_id>/<path:filename>")
def screenshots(run_id, filename):
    directory = os.path.join(_SCREENSHOT_DIR, run_id)
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=config.PORT, threaded=True)
