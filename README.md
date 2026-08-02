# qa-engine

AI-driven web-app QA engine. Point it at a site (or a scripted user flow) and
it streams back accessibility, security, and UX findings — while spending as
few model tokens as possible.

## How it works — token-frugal tiering

Most of the work is done for free by deterministic scanners; a small model
triages what's left; a strong model looks deeply only at what actually warrants
it.

- **Tier 0 — deterministic, free (~70% of the work).**
  - WCAG: [axe-core](tier0/vendor/axe.min.js) injected into the live page.
  - Security: response-header checks (CSP, HSTS, cookie flags…), TLS-cert
    expiry, and a redacting secret scanner.
  - UX: Lighthouse (performance / a11y / best-practices), run via the Node CLI.
- **Tier 1 — Haiku triage.** Reads each page's semantic skeleton + the Tier-0
  results and decides which items deserve deeper analysis. It never deletes a
  Tier-0 finding; it only gates *extra* model work.
- **Tier 2 — Sonnet deep review.** Rubric-driven review of the flagged items
  only (WCAG 2.2, OWASP, or — for flows — Nielsen's heuristics over screenshots).
- **Tier 3 — Opus.** A rare escalation seam for the hardest cases.

Extra frugality: a **content-hash cache** (`db.py`) keys findings by URL +
DOM-digest, so re-running an unchanged page replays its findings with zero model
calls; the crawler **deduplicates templates** so 100 product pages cost one
review, not a hundred; multi-page runs use the **Batch API** for throughput.

## Three pipelines

- **Page** (`POST /api/run/page`, body `{"url": "..."}`) — crawls the site and
  runs Security + WCAG.
- **Flow** (`POST /api/run/flow`, body `{"flow": {start_url, name, steps:[…]}}`)
  — drives a scripted flow in a real browser, capturing downscaled screenshots,
  and runs the UX heuristic review.
- **Repo** (`POST /api/run/repo`, body `{"repo": "owner/name", "branch":
  "bug-lab", "base": "main"}`) — clones a branch and reviews the *source*:
  Tier 0 runs deterministic scanners (secret patterns, conflict markers,
  breakpoints, shell/eval/pickle/SQL-interpolation/TLS-off risk surfaces),
  Haiku triages which files deserve deep review, and Sonnet code-reviews
  those files. With `base` set it runs in **diff mode**: only files changed
  since the merge base are candidates and the reviewer reads each file's
  unified diff as before-vs-after — pointed at a seeded-regression branch
  (the QA KSink `bug-lab`), it comes back with the seeded bugs. `repo`
  accepts `owner/name` (GitHub), a full URL, or a local path; the engine
  only clones and reads — it never executes repo code.

Both stream results live over **Server-Sent Events**; the single-page UI
(`templates/index.html`) renders a live stage tracker and severity-coded finding
cards.

## Layout

```
app.py              Flask + SSE endpoints, health check, screenshot serving
config.py           every cost knob (models, token caps, crawl budget, cache TTL)
db.py               SQLite: runs, findings, content-hash page_cache
digest.py           HTML -> semantic skeleton (+ shape/content hashes)
crawler.py          Playwright BFS crawler with template dedup
repo_scan.py        git plumbing: clone/branch/merge-base diff + file inventory
models.py           Anthropic wrapper: prompt caching + Batch API + mock mode
tier0/security.py   headers / TLS / secret scanning
tier0/repo.py       deterministic source checks (reuses the secret scanner)
tier0/wcag.py       axe-core injection (vendored axe.min.js)
tier0/ux.py         Lighthouse subprocess (graceful-skip when unavailable)
rubrics/            triage.md, wcag.md, owasp.md, nielsen.md,
                    repo-triage.md, code.md (cached system prompts)
pipeline_page.py    crawl -> tier0 -> cache -> triage -> deep review
pipeline_flow.py    drive flow -> capture (Pillow downscale) -> vision review
pipeline_repo.py    clone/diff -> tier0 scan -> triage files -> code review
pipelines/          thin SSE adapters (framing + heartbeats)
templates/index.html  the UI
ecosystem.config.cjs  pm2 (fork mode, gunicorn gthread)
bin/qa-engine       operate CLI (redeploy/restart/logs/backup)
```

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
npm install                     # Lighthouse (optional; UX tier degrades without it)
export ANTHROPIC_API_KEY=...     # omit to run in mock-model mode (Tier 0 still real)
.venv/bin/python app.py          # http://127.0.0.1:8044
```

**Mock mode.** With no `ANTHROPIC_API_KEY` (or `MOCK_MODELS=1`), the crawler,
Tier-0 scanners, digest, cache, screenshots, and SSE all run for real while the
Haiku/Sonnet tiers return canned results — enough to exercise and smoke-test the
whole system with no key.

## Tests

```bash
.venv/bin/python -m pytest tests/unit -q          # unit tests
MOCK_MODELS=1 .venv/bin/python -m tests.smoke_page   # end-to-end page run
MOCK_MODELS=1 .venv/bin/python -m tests.smoke_flow   # end-to-end flow run
MOCK_MODELS=1 .venv/bin/python -m tests.smoke_repo   # end-to-end repo run
```

## Deploying

On the lab980 droplet, see [DEPLOY.md](DEPLOY.md) — provisioned with the shared
`provision-site` tool and run under pm2 on `qa-engine.lab980.com`.
