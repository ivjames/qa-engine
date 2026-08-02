"""Repo pipeline: clone a branch (optionally diffed against a base branch)
-> Tier 0 deterministic source scan -> Tier 1 Haiku triage picks the files
worth deep review -> Tier 2 Sonnet code review of those files.

Diff mode is the high-signal path: given `base`, only files changed since
the merge base are candidates and the deep reviewer reads each file's diff
alongside its content — pointed at a seeded-regression branch it should
come back with the seeded bugs.

Async generator of event dicts (same SSE contract as the page pipeline).
git subprocess calls are blocking, so they run in an executor to keep the
event loop (and SSE heartbeats) alive.
"""

from __future__ import annotations

import asyncio
import time

import config
import db
import repo_scan
from models import Models
from prompts import DEEP_REVIEW_SCHEMA, REPO_TRIAGE_SCHEMA, rubric
from tier0.repo import run_repo_checks


def _finding_event(finding: dict, fid) -> dict:
    return {
        "type": "finding",
        "id": fid,
        "url": finding.get("url"),
        "pipeline": finding.get("pipeline"),
        "tier": finding.get("tier"),
        "rule": finding.get("rule"),
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
        "evidence": finding.get("evidence", {}),
    }


def _escalates(item: dict) -> bool:
    if not item.get("worth_deep_review"):
        return False
    conf = item.get("confidence", 0) or 0
    return (conf >= config.TRIAGE_ESCALATE_MIN_CONFIDENCE
            or item.get("severity") in config.TRIAGE_ESCALATE_SEVERITIES)


def _add_usage(stats: dict, usage: dict, model: str) -> None:
    per_model = stats["tokens_by_model"].setdefault(
        model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    for k in ("input", "output", "cache_read", "cache_write"):
        n = usage.get(k, 0) or 0
        stats["tokens"][k] += n
        per_model[k] += n


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated at {limit} chars]"


def _triage_user(co, files, tier0_findings) -> str:
    parts = [f"Repository: {co.label()}", f"HEAD: {co.head_sha[:12]}"]

    parts.append("\nFile inventory:\n" + repo_scan.file_tree_summary(files))

    if tier0_findings:
        compact = [
            {"path": f["url"], "rule": f["rule"], "severity": f["severity"]}
            for f in tier0_findings
        ]
        parts.append(f"\nTier-0 findings:\n{compact}")
    else:
        parts.append("\nTier-0 findings: none")

    if co.diff_mode:
        full_diff = "".join(co.diffs.get(p, "") for p in (co.changed_files or []))
        parts.append(f"\nChanged files vs {co.base} (merge base "
                     f"{(co.base_sha or '')[:12]}):\n"
                     + "\n".join(co.changed_files or ["(none)"]))
        if full_diff and len(full_diff) <= config.REPO_MAX_DIFF_CHARS:
            parts.append("\nFull unified diff:\n" + full_diff)
        else:
            stats = [f"{p}: {co.diffs.get(p, '').count(chr(10))} diff lines"
                     for p in (co.changed_files or [])]
            parts.append("\nDiff too large to inline; per-file sizes:\n"
                         + "\n".join(stats))
    return "\n".join(parts)


def _deep_user(co, item, text: str) -> str:
    parts = [
        f"Repository: {co.label()}",
        f"File under review: {item['path']}",
        f"Triage reason: {item.get('reason', '')}",
    ]
    if co.diff_mode:
        diff = co.diffs.get(item["path"], "")
        if diff:
            parts.append("\nUnified diff vs the base branch (review hunks "
                         "as before-vs-after):\n"
                         + _clip(diff, config.REPO_MAX_DIFF_CHARS))
        else:
            parts.append("\n(No diff for this file — it is unchanged vs the "
                         "base; review the content itself.)")
    parts.append("\nCurrent file content:\n"
                 + _clip(text, config.REPO_MAX_FILE_CHARS))
    return "\n".join(parts)


async def run_repo_pipeline(run_id: str, repo: str, branch: str, base, spec):
    """Async generator of event dicts for a repo (code) run."""
    models = Models()
    loop = asyncio.get_event_loop()

    stats = {
        "pages": 0, "files": 0, "changed_files": 0,
        "findings_by_severity": {}, "duration_secs": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "tokens_by_model": {},
    }
    started = time.time()

    def _tally(f):
        s = f.get("severity", "info")
        stats["findings_by_severity"][s] = stats["findings_by_severity"].get(s, 0) + 1

    target = f"{repo}@{branch}" + (f" vs {base}" if base else "")
    yield {"type": "run_start", "run_id": run_id, "kind": "repo",
           "target": target, "ts": started}

    # ---- fetch: clone + optional diff (blocking git -> executor) ----
    yield {"type": "stage", "stage": "fetch", "status": "start"}
    try:
        co = await loop.run_in_executor(
            None, repo_scan.checkout, repo, branch, base)
    except repo_scan.RepoScanError as exc:
        yield {"type": "error", "scope": "fatal", "message": str(exc)}
        db.finish_run(run_id, "error", stats, error=str(exc))
        yield {"type": "done", "run_id": run_id, "status": "error", "stats": stats}
        return

    try:
        files = await loop.run_in_executor(None, repo_scan.list_files, co)
        if co.diff_mode:
            changed = set(co.changed_files or [])
            scan_files = [f for f in files if f.path in changed]
            stats["changed_files"] = len(changed)
        else:
            scan_files = files
        stats["files"] = len(files)
        note = f"{co.head_sha[:12]} — {len(files)} files"
        if co.diff_mode:
            note += f", {stats['changed_files']} changed vs {base}"
        yield {"type": "stage", "stage": "fetch", "status": "end", "note": note}

        # ---- Tier 0: deterministic source scan ----
        yield {"type": "stage", "stage": "scan", "status": "start"}
        tier0_findings: list[dict] = []
        file_texts: dict[str, str] = {}
        for index, rf in enumerate(scan_files, start=1):
            yield {"type": "page_progress", "index": index,
                   "total": len(scan_files), "url": rf.path,
                   "template_id": rf.language, "cached": False}
            text = repo_scan.read_text(co, rf.path)
            if text is None:
                continue
            stats["pages"] += 1
            file_texts[rf.path] = text
            for f in run_repo_checks(rf.path, text, rf.language):
                fid = db.add_finding(run_id, f)
                _tally(f)
                tier0_findings.append(f)
                yield _finding_event(f, fid)
        yield {"type": "stage", "stage": "scan", "status": "end",
               "note": f"{stats['pages']} files scanned, "
                       f"{len(tier0_findings)} findings"}

        # ---- Tier 1: Haiku triage (one call over the whole inventory) ----
        yield {"type": "stage", "stage": "triage", "status": "start"}
        known_paths = {f.path for f in scan_files}
        r = models.call(
            model=config.MODEL_TIER1, system_blocks=rubric("repo-triage.md"),
            user_content=_triage_user(co, scan_files, tier0_findings),
            max_tokens=config.TIER1_MAX_TOKENS,
            thinking=config.TIER1_THINKING, json_schema=REPO_TRIAGE_SCHEMA)
        _add_usage(stats, r.usage, config.MODEL_TIER1)
        items = (r.parsed or {}).get("items", [])
        escalated = [i for i in items
                     if _escalates(i) and i.get("path") in known_paths]
        escalated.sort(key=lambda i: i.get("confidence", 0) or 0, reverse=True)
        dropped = len(escalated) - config.REPO_MAX_DEEP_FILES
        escalated = escalated[:config.REPO_MAX_DEEP_FILES]
        note = f"{len(escalated)} file(s) escalated"
        if dropped > 0:
            note += f" ({dropped} over the deep-review cap, dropped)"
        yield {"type": "stage", "stage": "triage", "status": "end", "note": note}

        # ---- Tier 2: Sonnet deep review per escalated file ----
        if escalated:
            yield {"type": "stage", "stage": "deep_review", "status": "start"}
            code_rubric = rubric("code.md")

            def deep_kwargs(item):
                text = file_texts.get(item["path"])
                if text is None:
                    text = repo_scan.read_text(co, item["path"]) or ""
                return dict(
                    model=config.MODEL_TIER2, system_blocks=code_rubric,
                    user_content=_deep_user(co, item, text),
                    max_tokens=config.TIER2_MAX_TOKENS,
                    effort=config.TIER2_EFFORT, json_schema=DEEP_REVIEW_SCHEMA)

            deep_results = []
            if len(escalated) > config.BATCH_THRESHOLD:
                units = [
                    {"custom_id": f"r{i}",
                     "request": models.build_request(**deep_kwargs(item))}
                    for i, item in enumerate(escalated)]
                batch_id = models.submit_batch(units)
                async for ev in _await_batch(models, batch_id, "deep_review"):
                    yield ev
                res = models.batch_results(batch_id)
                deep_results = [(escalated[i], res.get(f"r{i}"))
                                for i in range(len(escalated))]
            else:
                for item in escalated:
                    deep_results.append((item, models.call(**deep_kwargs(item))))

            for item, r2 in deep_results:
                if not r2:
                    continue
                _add_usage(stats, r2.usage, config.MODEL_TIER2)
                for df in (r2.parsed or {}).get("findings", []):
                    rule = df.get("rule", "code/finding")
                    finding = {
                        "url": item["path"],
                        "pipeline": "security" if rule.startswith("security") else "code",
                        "tier": 2, "rule": rule,
                        "severity": df.get("severity", "moderate"),
                        "title": df.get("title", ""),
                        "detail": df.get("detail", ""),
                        "evidence": df.get("evidence", {}),
                    }
                    fid = db.add_finding(run_id, finding)
                    _tally(finding)
                    yield _finding_event(finding, fid)
            yield {"type": "stage", "stage": "deep_review", "status": "end"}
    finally:
        repo_scan.cleanup(co)

    stats["estimated_cost_usd"] = config.estimate_cost_usd(stats["tokens_by_model"])
    stats["duration_secs"] = round(time.time() - started, 2)
    db.finish_run(run_id, "done", stats)
    yield {"type": "done", "run_id": run_id, "status": "done", "stats": stats}


async def _await_batch(models, batch_id, stage):
    """Poll a batch, yielding pending stage events (keeps SSE alive)."""
    waited = 0
    while True:
        status = models.poll_batch(batch_id)
        counts = status.get("counts", {})
        done = counts.get("succeeded", 0) + counts.get("errored", 0)
        total = done + counts.get("processing", 0)
        yield {"type": "stage", "stage": stage, "status": "pending",
               "batch_id": batch_id, "done": done, "total": total}
        if status.get("status") == "ended":
            return
        if waited >= config.BATCH_MAX_WAIT_SECS:
            yield {"type": "error", "scope": "repo",
                   "message": f"batch {batch_id} timed out"}
            return
        await asyncio.sleep(config.BATCH_POLL_SECS)
        waited += config.BATCH_POLL_SECS
