"""Per-flow pipeline (UX): drive a scripted flow in Playwright, capture
downscaled screenshots, evaluate each against Nielsen's heuristics with the
Sonnet vision model. Yields event dicts (see pipelines.format_frame)."""

import base64
import io
import os
import time

import config
import db
from crawler import launch_browser
from models import Models
from PIL import Image
from playwright.async_api import async_playwright
from prompts import DEEP_REVIEW_SCHEMA, rubric

_VIEW_CHANGING = {"goto", "click", "press", "select", "screenshot"}


def _finding_event(finding: dict, fid) -> dict:
    return {
        "type": "finding", "id": fid, "url": finding.get("url"),
        "pipeline": "ux", "tier": finding.get("tier", 2),
        "rule": finding.get("rule"), "severity": finding.get("severity"),
        "title": finding.get("title"), "detail": finding.get("detail"),
        "evidence": finding.get("evidence", {}),
    }


async def _run_step(page, step: dict):
    """Execute one flow step. Raises on failure (caller records a step error)."""
    action = step.get("action")
    if action == "goto":
        await page.goto(step["url"], wait_until="domcontentloaded",
                        timeout=config.NAV_TIMEOUT_MS)
    elif action == "click":
        await page.click(step["selector"], timeout=config.NAV_TIMEOUT_MS)
    elif action == "fill":
        await page.fill(step["selector"], step.get("value", ""),
                        timeout=config.NAV_TIMEOUT_MS)
    elif action == "press":
        await page.press(step["selector"], step["key"], timeout=config.NAV_TIMEOUT_MS)
    elif action == "select":
        await page.select_option(step["selector"], step.get("value"),
                                 timeout=config.NAV_TIMEOUT_MS)
    elif action == "wait":
        await page.wait_for_timeout(int(step.get("ms", 500)))
    elif action == "screenshot":
        pass  # capture happens below
    else:
        raise ValueError(f"unknown action: {action!r}")


async def _capture(page, run_id: str, idx: int, label: str) -> dict:
    """Screenshot -> Pillow downscale to <=SCREENSHOT_MAX_WIDTH -> JPEG q70.
    Saves to data/screenshots/<run>/<idx>.jpg and returns base64 for the model."""
    png = await page.screenshot()
    img = Image.open(io.BytesIO(png))
    if img.width > config.SCREENSHOT_MAX_WIDTH:
        h = round(img.height * config.SCREENSHOT_MAX_WIDTH / img.width)
        img = img.resize((config.SCREENSHOT_MAX_WIDTH, h), Image.LANCZOS)
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format=config.SCREENSHOT_FORMAT, quality=config.SCREENSHOT_QUALITY)
    jpeg = buf.getvalue()

    out_dir = os.path.join(os.path.dirname(config.DB_PATH), "screenshots", run_id)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{idx:02d}.jpg"
    with open(os.path.join(out_dir, fname), "wb") as fh:
        fh.write(jpeg)

    return {"label": label, "path": f"/screenshots/{run_id}/{fname}",
            "b64": base64.b64encode(jpeg).decode()}


async def run_flow_pipeline(run_id: str, flow_spec: dict):
    """Async generator of event dicts for a flow (UX) run."""
    flow_spec = flow_spec or {}
    steps = flow_spec.get("steps", [])
    start_url = flow_spec.get("start_url")
    name = flow_spec.get("name", "flow")
    models = Models()

    stats = {"steps": 0, "screenshots": 0, "findings_by_severity": {},
             "duration_secs": 0,
             "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
             "tokens_by_model": {}}
    started = time.time()

    def _tally(f):
        s = f.get("severity", "info")
        stats["findings_by_severity"][s] = stats["findings_by_severity"].get(s, 0) + 1

    yield {"type": "run_start", "run_id": run_id, "kind": "flow",
           "target": start_url or name, "ts": started}

    captures = []
    yield {"type": "stage", "stage": "drive", "status": "start"}
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            page = await browser.new_page()
            if start_url:
                await page.goto(start_url, wait_until="domcontentloaded",
                                timeout=config.NAV_TIMEOUT_MS)
            total = len(steps)
            for i, step in enumerate(steps):
                stats["steps"] += 1
                label = step.get("label") or f"{step.get('action', 'step')} {i + 1}"
                try:
                    await _run_step(page, step)
                except Exception as exc:
                    yield {"type": "error", "scope": "step",
                           "message": f"{label}: {exc}"}
                if step.get("action") in _VIEW_CHANGING:
                    try:
                        cap = await _capture(page, run_id, i, label)
                        captures.append(cap)
                        stats["screenshots"] += 1
                        yield {"type": "page_progress", "index": i + 1,
                               "total": total, "label": label,
                               "screenshot": cap["path"]}
                    except Exception as exc:
                        yield {"type": "error", "scope": "step",
                               "message": f"capture {label}: {exc}"}
            # always capture a final frame
            try:
                cap = await _capture(page, run_id, len(steps), "final")
                captures.append(cap)
                stats["screenshots"] += 1
                yield {"type": "page_progress", "index": total, "total": total,
                       "label": "final", "screenshot": cap["path"]}
            except Exception:
                pass
            await page.close()
        finally:
            await browser.close()
    yield {"type": "stage", "stage": "drive", "status": "end",
           "note": f"{stats['screenshots']} screenshots"}

    # ---- Tier 2 vision review per captured step ----
    if captures:
        yield {"type": "stage", "stage": "ux_review", "status": "start"}
        nielsen = rubric("nielsen.md")
        for cap in captures:
            user_content = [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": cap["b64"]}},
                {"type": "text", "text": (
                    f"Step: {cap['label']}. Flow: {name}. Evaluate this screen "
                    f"against Nielsen's 10 heuristics. JSON only.")},
            ]
            r = models.call(model=config.MODEL_TIER2, system_blocks=nielsen,
                            user_content=user_content,
                            max_tokens=config.VISION_MAX_TOKENS,
                            effort=config.TIER2_EFFORT, json_schema=DEEP_REVIEW_SCHEMA)
            vision_tok = stats["tokens_by_model"].setdefault(
                config.MODEL_TIER2,
                {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            for k in ("input", "output", "cache_read", "cache_write"):
                n = r.usage.get(k, 0) or 0
                stats["tokens"][k] += n
                vision_tok[k] += n
            for df in (r.parsed or {}).get("findings", []):
                finding = {
                    "url": cap["label"], "pipeline": "ux", "tier": 2,
                    "rule": f"ux/{df.get('rule', 'heuristic')}",
                    "severity": df.get("severity", "moderate"),
                    "title": df.get("title", ""), "detail": df.get("detail", ""),
                    "evidence": {**df.get("evidence", {}), "screenshot": cap["path"]},
                }
                fid = db.add_finding(run_id, finding)
                _tally(finding)
                yield _finding_event(finding, fid)
        yield {"type": "stage", "stage": "ux_review", "status": "end"}

    stats["estimated_cost_usd"] = config.estimate_cost_usd(stats["tokens_by_model"])
    stats["duration_secs"] = round(time.time() - started, 2)
    db.finish_run(run_id, "done", stats)
    yield {"type": "done", "run_id": run_id, "status": "done", "stats": stats}
