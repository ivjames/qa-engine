"""SSE adapter for the page pipeline: create the run row, then stream the async
pipeline's events as SSE frames. No domain logic lives here."""

import db
from pipeline_page import run_page_pipeline
from pipelines import stream_async


def sse_page(seed_url: str, spec: dict):
    """Return a sync generator of SSE frame strings for a page run."""
    db.init_db()
    run_id = db.create_run("page", seed_url, spec or {})
    return stream_async(lambda: run_page_pipeline(run_id, seed_url, spec or {}))
