"""SSE adapter for the repo pipeline: create the run row, then stream the
async pipeline's events as SSE frames. No domain logic lives here."""

import db
from pipeline_repo import run_repo_pipeline
from pipelines import stream_async


def sse_repo(repo: str, branch: str, base, spec: dict):
    """Return a sync generator of SSE frame strings for a repo run."""
    db.init_db()
    target = f"{repo}@{branch}" + (f" vs {base}" if base else "")
    run_id = db.create_run("repo", target, {
        "repo": repo, "branch": branch, "base": base, **(spec or {})})
    return stream_async(
        lambda: run_repo_pipeline(run_id, repo, branch, base, spec or {}))
