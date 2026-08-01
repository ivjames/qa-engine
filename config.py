"""qa-engine configuration. Every cost knob lives here — nothing else may
hard-code a model id, token cap, or budget. Env vars override where noted."""

import os
import glob as _glob


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---- models (tier -> model id) ----
MODEL_TIER1 = "claude-haiku-4-5"   # triage
MODEL_TIER2 = "claude-sonnet-5"    # deep review + vision
MODEL_TIER3 = "claude-opus-5"      # rare escalation

# ---- per-tier output caps ----
TIER1_MAX_TOKENS = 1024
TIER2_MAX_TOKENS = 2048
TIER3_MAX_TOKENS = 4096
VISION_MAX_TOKENS = 1536

# Reasoning knobs, translated per model family by models.py:
# Haiku 4.5 takes thinking={"type": ...}; Sonnet/Opus 5 take output_config effort.
TIER1_THINKING = "disabled"
TIER2_EFFORT = "low"
TIER3_EFFORT = "medium"

# ---- crawl budget ----
MAX_PAGES = _env_int("QA_MAX_PAGES", 25)        # hard page cap per run
MAX_DEPTH = _env_int("QA_MAX_DEPTH", 3)         # BFS depth from seed
MAX_TEMPLATES = _env_int("QA_MAX_TEMPLATES", 12)  # distinct templates sent to LLMs
CRAWL_CONCURRENCY = 3
NAV_TIMEOUT_MS = 15000

# ---- batch API ----
BATCH_THRESHOLD = 8         # > this many units in a stage -> use Batch API
BATCH_POLL_SECS = 5
BATCH_MAX_WAIT_SECS = 1200


# ---- pricing (USD per 1M tokens: input, output) ----
# List prices as of 2026-08. Cache reads bill at ~0.1x input, cache writes at
# ~1.25x input (5-minute TTL). Sonnet 5 has intro pricing ($2/$10) through
# 2026-08-31; the list price is used here so estimates don't silently change.
MODEL_PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def estimate_cost_usd(tokens_by_model):
    """Estimated USD cost of a run from its per-model token tallies:
    {model_id: {input, output, cache_read, cache_write}}. Models with no
    price entry contribute 0 (better to under-report than to guess)."""
    total = 0.0
    for model, tok in (tokens_by_model or {}).items():
        prices = MODEL_PRICES.get(model)
        if not prices:
            continue
        in_price, out_price = prices
        total += (
            (tok.get("input", 0) or 0) * in_price
            + (tok.get("output", 0) or 0) * out_price
            + (tok.get("cache_read", 0) or 0) * in_price * CACHE_READ_MULT
            + (tok.get("cache_write", 0) or 0) * in_price * CACHE_WRITE_MULT
        ) / 1_000_000
    return round(total, 4)

# ---- tiering thresholds ----
TRIAGE_ESCALATE_MIN_CONFIDENCE = 0.5
TRIAGE_ESCALATE_SEVERITIES = {"serious", "critical"}

# ---- flow screenshots ----
SCREENSHOT_MAX_WIDTH = 1024
SCREENSHOT_FORMAT = "JPEG"
SCREENSHOT_QUALITY = 70

# ---- server ----
PORT = _env_int("PORT", 8044)
DB_PATH = os.environ.get("QA_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "qa.db"))
MAX_CONCURRENT_RUNS = 2
SSE_HEARTBEAT_SECS = 20     # nginx vhost proxies with a 60s read timeout

# ---- lighthouse (tier0 UX) ----
LIGHTHOUSE_TIMEOUT_SECS = 90
LIGHTHOUSE_CATEGORIES = ("performance", "accessibility", "best-practices")
LIGHTHOUSE_SCORE_SERIOUS = 0.5   # category score below -> serious
LIGHTHOUSE_SCORE_MODERATE = 0.9  # category score below -> moderate

# ---- mock / keyless mode ----
MOCK_MODELS = os.environ.get("MOCK_MODELS", "") == "1" or not os.environ.get("ANTHROPIC_API_KEY")


def chrome_path():
    """Chrome binary for Lighthouse: explicit CHROME_PATH env, else the newest
    Playwright-managed chromium, else None (Lighthouse then finds its own)."""
    explicit = os.environ.get("CHROME_PATH")
    if explicit:
        return explicit
    roots = []
    pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if pw_root:
        roots.append(pw_root)
    roots += [os.path.expanduser("~/.cache/ms-playwright"), "/opt/pw-browsers"]
    for root in roots:
        hits = sorted(_glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome")))
        if hits:
            return hits[-1]
    return None
