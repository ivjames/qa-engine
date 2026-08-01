"""End-to-end flow-pipeline smoke test (keyless / MOCK_MODELS).

Serves the fixtures, drives a small scripted flow, and asserts screenshots are
captured, downscaled to <= config.SCREENSHOT_MAX_WIDTH, saved as JPEG, and the
SSE stream ends in `done`. Run:

    MOCK_MODELS=1 .venv/bin/python -m tests.smoke_flow
"""

import io
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
    config.DB_PATH = os.path.join(FIXTURES, "_smoke_flow.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(config.DB_PATH + suffix)
        except FileNotFoundError:
            pass
    import db
    db.reset()
    db.init_db()

    from PIL import Image
    from pipelines.flow import sse_flow

    httpd, port = _serve()
    try:
        flow = {
            "name": "smoke",
            "start_url": f"http://127.0.0.1:{port}/sample.html",
            "steps": [
                {"action": "fill", "selector": "input[name=q]", "value": "hello",
                 "label": "type query"},
                {"action": "click", "selector": "button[type=submit]",
                 "label": "submit search"},
                {"action": "screenshot", "label": "result"},
            ],
        }
        events = _collect(sse_flow(flow))
        types = [e[0] for e in events]
        assert types[0] == "run_start"
        assert types[-1] == "done", types[-3:]

        progress = [e[1] for e in events if e[0] == "page_progress"]
        shots = [p["screenshot"] for p in progress if p.get("screenshot")]
        assert shots, "no screenshots captured"

        # verify a saved screenshot is JPEG and downscaled
        run_id = events[0][1]["run_id"]
        shot_dir = os.path.join(os.path.dirname(config.DB_PATH), "screenshots", run_id)
        files = sorted(os.listdir(shot_dir))
        assert files, "no screenshot files on disk"
        with open(os.path.join(shot_dir, files[0]), "rb") as fh:
            img = Image.open(io.BytesIO(fh.read()))
            assert img.format == "JPEG", img.format
            assert img.width <= config.SCREENSHOT_MAX_WIDTH, img.width

        print(f"OK  frames={len(events)}  screenshots={len(shots)}  "
              f"first={files[0]} size={img.size} fmt={img.format}")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
