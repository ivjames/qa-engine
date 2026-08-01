# Deploying qa-engine

qa-engine is a Flask + SSE app (gunicorn under pm2) served on the shared lab980
droplet as `qa-engine.lab980.com`, local port **8044**. It follows the standard
lab980 shape: one dir per site (`/var/www/qa-engine`), config + `data/` in the
app dir, pm2 in **fork** mode, nginx + TLS written by the shared `provision-site`
tool (this repo ships **no** vhost or provision script of its own).

It also uses two extra runtimes beyond a plain Node site: a **Python venv**
(Flask/Playwright/Pillow/anthropic) and **Node** (Lighthouse CLI, driven by
`tier0/ux.py`). Playwright drives a headless Chromium for crawling, axe-core
injection, and flow screenshots.

## First-time provision (on the droplet, as root)

```bash
# 1. Scaffold DNS + dir + repo clone + nginx vhost + TLS (shared lab980 tool).
provision-site qa-engine ivjames/qa-engine --port 8044

cd /var/www/qa-engine

# 2. Python side: venv + deps + the Playwright browser (with OS deps).
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium

# 3. Node side: Lighthouse.
npm install

# 4. Env. provision-site already seeded PORT=8044 into .env.
#    ANTHROPIC_API_KEY comes from /etc/environment on this droplet — make sure
#    pm2 inherits it. Either export it before `pm2 start`, or add it to .env:
grep -q ANTHROPIC_API_KEY .env || \
  echo "ANTHROPIC_API_KEY=$(. /etc/environment; echo "$ANTHROPIC_API_KEY")" >> .env

# 5. Start under pm2 (fork mode via ecosystem.config.cjs) and persist.
pm2 start ecosystem.config.cjs
pm2 save

# 6. Put the operate CLI on PATH.
ln -sf /var/www/qa-engine/bin/qa-engine /usr/local/bin/qa-engine

# 7. Smoke it.
curl -s https://qa-engine.lab980.com/healthz
```

If the boot hook was never installed on this droplet, do it once (survives
reboot): `pm2 startup systemd -u root --hp /root` then run the line it prints;
confirm `systemctl is-enabled pm2-root` → enabled.

## Redeploying

```bash
qa-engine redeploy      # git pull -> pip install -> npm install -> playwright install -> pm2 restart
qa-engine restart
qa-engine logs
qa-engine backup        # tar data/qa.db + screenshots into data/backups/
```

## Environment / config

Everything tunable lives in `config.py`, overridable via env (`.env`):

- `PORT` — local bind port (8044).
- `ANTHROPIC_API_KEY` — from `/etc/environment`. **If unset, the app runs in
  mock-model mode**: Tier 0 (axe/security/Lighthouse), crawling, digests, the
  cache, and SSE all work for real; the Haiku/Sonnet tiers return canned
  results. Good for a smoke test, not for real reviews.
- `MOCK_MODELS=1` — force mock mode even with a key present.
- `CHROME_PATH` — Chrome binary for Lighthouse; blank auto-detects Playwright's
  chromium.
- `QA_MAX_PAGES` / `QA_MAX_DEPTH` / `QA_MAX_TEMPLATES` — crawl budget.

## Notes on the deployment shape

- **pm2 fork mode only.** `ecosystem.config.cjs` sets `exec_mode: "fork"` and
  never `instances` (cluster mode is a known lab980 foot-gun). gunicorn owns
  concurrency via `--worker-class gthread --workers 1 --threads 4`.
- **SSE + nginx.** The `provision-site` vhost proxies with `proxy_read_timeout
  60s`; the app emits heartbeats well inside that window, and gunicorn runs with
  `--timeout 0` so long crawls aren't killed.
- **Node version.** Lighthouse 11 supports Node 18+, so it's fine on the
  droplet's Node 20 today and after the planned Node 22 bump. After a Node
  upgrade, re-run `npm install` and `.venv/bin/playwright install chromium`.
