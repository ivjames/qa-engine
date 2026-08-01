"""SSE adapter for the flow pipeline: create the run row, then stream the async
pipeline's events as SSE frames. No domain logic lives here."""

import db
from pipeline_flow import run_flow_pipeline
from pipelines import stream_async


def sse_flow(flow_spec: dict):
    """Return a sync generator of SSE frame strings for a flow run."""
    db.init_db()
    target = (flow_spec or {}).get("start_url") or (flow_spec or {}).get("name", "flow")
    run_id = db.create_run("flow", target, flow_spec or {})
    return stream_async(lambda: run_flow_pipeline(run_id, flow_spec or {}))
