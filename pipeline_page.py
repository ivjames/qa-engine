"""Per-page pipeline: crawl -> Tier 0 (deterministic) -> Tier 1 Haiku triage
-> Tier 2 Sonnet deep review. Yields event dicts (see pipelines.format_frame
for the SSE contract). Every run reviews fresh — there is deliberately no
cross-run result cache, so the run history at /runs always reflects the site
as it was at scan time (a DOM-keyed cache used to replay stale security/perf
findings after non-DOM fixes).

Async generator: awaits the Playwright crawler and axe injection; Lighthouse
(a blocking subprocess) is run in an executor. The Flask layer drives this in
a worker-thread event loop (see pipelines.stream_async)."""

import asyncio
import time

import config
import db
from crawler import crawl, launch_browser
from digest import semantic_skeleton
from models import Models
from playwright.async_api import async_playwright
from prompts import DEEP_REVIEW_SCHEMA, TRIAGE_SCHEMA, rubric
from tier0.security import run_security
from tier0.ux import run_lighthouse
from tier0.wcag import run_axe

_RUBRIC_FILE = {"wcag": "wcag.md", "security": "owasp.md"}


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
    sev = item.get("severity")
    conf = item.get("confidence", 0) or 0
    return conf >= config.TRIAGE_ESCALATE_MIN_CONFIDENCE or sev in config.TRIAGE_ESCALATE_SEVERITIES


def _add_usage(stats: dict, usage: dict, model: str) -> None:
    per_model = stats["tokens_by_model"].setdefault(
        model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    for k in ("input", "output", "cache_read", "cache_write"):
        n = usage.get(k, 0) or 0
        stats["tokens"][k] += n
        per_model[k] += n


async def run_page_pipeline(run_id: str, seed_url: str, spec: dict):
    """Async generator of event dicts for a page (Security + WCAG) run."""
    spec = spec or {}
    run_ux = spec.get("ux", True)
    models = Models()

    stats = {
        "pages": 0, "templates": 0,
        "findings_by_severity": {}, "duration_secs": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "tokens_by_model": {},
    }
    started = time.time()

    def _tally(f):
        s = f.get("severity", "info")
        stats["findings_by_severity"][s] = stats["findings_by_severity"].get(s, 0) + 1

    yield {"type": "run_start", "run_id": run_id, "kind": "page",
           "target": seed_url, "ts": started}

    # template_key -> {template_id, url, tier0, skeleton, findings}
    templates: dict[str, dict] = {}
    loop = asyncio.get_event_loop()

    yield {"type": "stage", "stage": "crawl", "status": "start"}
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            index = 0
            async for unit in crawl(seed_url, browser=browser):
                index += 1
                stats["pages"] += 1
                yield {"type": "page_progress", "index": index,
                       "total": config.MAX_PAGES, "url": unit.url,
                       "template_id": unit.template_id,
                       "cached": unit.template_id in templates}
                if unit.error:
                    yield {"type": "error", "scope": "page",
                           "message": unit.error, "url": unit.url}
                    continue
                if not unit.is_new_template:
                    continue

                stats["templates"] += 1
                skeleton = semantic_skeleton(unit.html, unit.url)

                # ---- Tier 0 (deterministic, free) ----
                tier0: list[dict] = []
                tier0 += run_security(unit.url, unit.resp_headers, unit.tls, unit.html, {})
                page = await browser.new_page()
                try:
                    await page.goto(unit.url, wait_until="domcontentloaded",
                                    timeout=config.NAV_TIMEOUT_MS)
                    tier0 += await run_axe(page, unit.url)
                except Exception as exc:  # axe/nav failure is non-fatal
                    yield {"type": "error", "scope": "page",
                           "message": f"axe: {exc}", "url": unit.url}
                finally:
                    await page.close()
                if run_ux:
                    tier0 += await loop.run_in_executor(None, run_lighthouse, unit.url)

                template_findings = []
                for f in tier0:
                    fid = db.add_finding(run_id, f)
                    _tally(f)
                    template_findings.append(f)
                    yield _finding_event(f, fid)

                templates[unit.template_id] = {
                    "template_id": unit.template_id, "url": unit.url,
                    "skeleton": skeleton, "tier0": tier0,
                    "findings": template_findings,
                }
        finally:
            await browser.close()
    yield {"type": "stage", "stage": "crawl", "status": "end",
           "note": f"{stats['templates']} templates / {stats['pages']} pages"}

    # ---- Tier 1: Haiku triage (one unit per template) ----
    triage_targets = [t for t in templates.values() if t["tier0"]]
    triage_verdicts: dict[str, list] = {}
    if triage_targets:
        yield {"type": "stage", "stage": "triage", "status": "start"}
        triage_rubric = rubric("triage.md")

        def triage_user(t):
            compact = [{"rule": f["rule"], "pipeline": f["pipeline"],
                        "severity": f["severity"]} for f in t["tier0"]]
            return (f"URL: {t['url']}\n\nSemantic skeleton:\n{t['skeleton']}\n\n"
                    f"Tier-0 findings:\n{compact}")

        if len(triage_targets) > config.BATCH_THRESHOLD:
            units = []
            for i, t in enumerate(triage_targets):
                req = models.build_request(
                    model=config.MODEL_TIER1, system_blocks=triage_rubric,
                    user_content=triage_user(t), max_tokens=config.TIER1_MAX_TOKENS,
                    thinking=config.TIER1_THINKING, json_schema=TRIAGE_SCHEMA)
                units.append({"custom_id": f"t{i}", "request": req})
            batch_id = models.submit_batch(units)
            async for ev in _await_batch(models, batch_id, "triage"):
                yield ev
            results = models.batch_results(batch_id)
            for i, t in enumerate(triage_targets):
                r = results.get(f"t{i}")
                if r:
                    _add_usage(stats, r.usage, config.MODEL_TIER1)
                    triage_verdicts[t["template_id"]] = (r.parsed or {}).get("items", [])
        else:
            for t in triage_targets:
                r = models.call(
                    model=config.MODEL_TIER1, system_blocks=triage_rubric,
                    user_content=triage_user(t), max_tokens=config.TIER1_MAX_TOKENS,
                    thinking=config.TIER1_THINKING, json_schema=TRIAGE_SCHEMA)
                _add_usage(stats, r.usage, config.MODEL_TIER1)
                triage_verdicts[t["template_id"]] = (r.parsed or {}).get("items", [])
        yield {"type": "stage", "stage": "triage", "status": "end"}

    # ---- Tier 2: Sonnet deep review on escalated items ----
    escalations = []  # (template, item)
    for t in triage_targets:
        for item in triage_verdicts.get(t["template_id"], []):
            if _escalates(item):
                escalations.append((t, item))

    if escalations:
        yield {"type": "stage", "stage": "deep_review", "status": "start"}

        def deep_user(t, item):
            return (f"URL: {t['url']}\n\nSemantic skeleton:\n{t['skeleton']}\n\n"
                    f"Flagged item to review deeply:\n{item}")

        def rubric_for(item):
            return rubric(_RUBRIC_FILE.get(item.get("pipeline"), "owasp.md"))

        deep_results = []
        if len(escalations) > config.BATCH_THRESHOLD:
            units = []
            for i, (t, item) in enumerate(escalations):
                req = models.build_request(
                    model=config.MODEL_TIER2, system_blocks=rubric_for(item),
                    user_content=deep_user(t, item), max_tokens=config.TIER2_MAX_TOKENS,
                    effort=config.TIER2_EFFORT, json_schema=DEEP_REVIEW_SCHEMA)
                units.append({"custom_id": f"d{i}", "request": req})
            batch_id = models.submit_batch(units)
            async for ev in _await_batch(models, batch_id, "deep_review"):
                yield ev
            res = models.batch_results(batch_id)
            deep_results = [(escalations[i][0], res.get(f"d{i}"))
                            for i in range(len(escalations))]
        else:
            for t, item in escalations:
                r = models.call(
                    model=config.MODEL_TIER2, system_blocks=rubric_for(item),
                    user_content=deep_user(t, item), max_tokens=config.TIER2_MAX_TOKENS,
                    effort=config.TIER2_EFFORT, json_schema=DEEP_REVIEW_SCHEMA)
                deep_results.append((t, r))

        for t, r in deep_results:
            if not r:
                continue
            _add_usage(stats, r.usage, config.MODEL_TIER2)
            for df in (r.parsed or {}).get("findings", []):
                finding = {
                    "url": t["url"], "pipeline": _pipeline_of(df, t),
                    "tier": 2, "rule": df.get("rule", "deep/finding"),
                    "severity": df.get("severity", "moderate"),
                    "title": df.get("title", ""), "detail": df.get("detail", ""),
                    "evidence": df.get("evidence", {}),
                }
                fid = db.add_finding(run_id, finding)
                _tally(finding)
                t["findings"].append(finding)
                yield _finding_event(finding, fid)
        yield {"type": "stage", "stage": "deep_review", "status": "end"}

    stats["estimated_cost_usd"] = config.estimate_cost_usd(stats["tokens_by_model"])
    stats["duration_secs"] = round(time.time() - started, 2)
    db.finish_run(run_id, "done", stats)
    yield {"type": "done", "run_id": run_id, "status": "done", "stats": stats}


def _pipeline_of(deep_finding: dict, template: dict) -> str:
    rule = deep_finding.get("rule", "")
    if rule.startswith("wcag"):
        return "wcag"
    if rule.startswith("security"):
        return "security"
    if rule.startswith("ux"):
        return "ux"
    return "security"


async def _await_batch(models, batch_id, stage):
    """Poll a batch, yielding pending stage events (also keeps SSE alive)."""
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
            yield {"type": "error", "scope": "page",
                   "message": f"batch {batch_id} timed out"}
            return
        await asyncio.sleep(config.BATCH_POLL_SECS)
        waited += config.BATCH_POLL_SECS
