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
npm ci

# 4. Env. provision-site already seeded PORT=8044 into .env. Add the key
#    with an editor (see "Environment / config" below) and lock the file down.
#    config.py reads .env itself on start; nothing is inherited from the shell.
${EDITOR:-nano} .env        # add: ANTHROPIC_API_KEY=sk-ant-...
chmod 600 .env

# 5. Put the operate CLI on PATH, then let its first deploy do the first
#    `pm2 start` (from ecosystem.config.cjs, scrubbed environment), probe, save.
ln -sf /var/www/qa-engine/bin/qa-engine /usr/local/bin/qa-engine
qa-engine deploy

# 6. Smoke it.
curl -s https://qa-engine.lab980.com/healthz
```

Steps 2-3 are what `qa-engine deploy` repeats every time (it creates the venv
if missing, `pip install`s, `npm ci`s, and installs Playwright's chromium only
when its install location is absent), so on a fresh box you can skip them and
go straight to step 5 — they are listed so the OS-level `--with-deps` run,
which needs apt and is done once, is not forgotten.

If the boot hook was never installed on this droplet, do it once (survives
reboot): `pm2 startup systemd -u root --hp /root` then run the line it prints;
confirm `systemctl is-enabled pm2-root` → enabled.

## Locking it down (nginx basic auth — REQUIRED)

> **Not applied. Verified from outside on 2026-09-07:**
>
> ```
> GET https://qa-engine.lab980.com/         -> 200   (this section's own check expects 401)
> GET https://qa-engine.lab980.com/runs     -> 200
> GET https://qa-engine.lab980.com/healthz  -> 200   {"mock_models":false,"status":"ok"}
> ```
>
> `"mock_models":false` means a real `ANTHROPIC_API_KEY` is loaded, so the
> budget exposure described below is live rather than hypothetical. The gate is
> an nginx change on the droplet and cannot be made from this repo. Until
> someone runs the steps below on the box, treat this site as public.

The app itself has **no auth**, and the hostname is not a secret: every TLS
cert lands in public Certificate Transparency logs, so assume the subdomain is
known to scanners. Unauthenticated, a visitor can spend Anthropic API budget
and aim the crawler at arbitrary third-party URLs (`POST /api/run/*`), read
the full findings history for our own sites (`/runs`), and delete runs. The
fix lives in nginx, not the app — operator-only tools on this droplet get
vhost-level basic auth:

```bash
# 1. Credentials file (htpasswd ships with apache2-utils).
apt-get install -y apache2-utils
htpasswd -c /etc/nginx/htpasswd-qa-engine ivan     # prompts for a password

# 2. Edit the vhost provision-site wrote (sites-available/qa-engine…).
#    Inside the server{} block add:
#
#      auth_basic "qa-engine";
#      auth_basic_user_file /etc/nginx/htpasswd-qa-engine;
#
#      # health check stays open for probes — duplicate the proxy_* lines
#      # from the main location / block here:
#      location = /healthz {
#          auth_basic off;
#          proxy_pass http://127.0.0.1:8044;
#      }

# 3. Validate + reload, then verify both sides of the gate.
nginx -t && systemctl reload nginx
curl -s -o /dev/null -w '%{http_code}\n' https://qa-engine.lab980.com/        # 401
curl -s https://qa-engine.lab980.com/healthz                                  # 200, no auth
curl -su ivan https://qa-engine.lab980.com/runs -o /dev/null -w '%{http_code}\n'  # 200
```

Browsers cache the credentials for the session and attach them to same-origin
`fetch()` calls, so the SSE run UI works unchanged behind the gate. There are
no webhooks or cross-origin callers on this app, so nothing else needs an
exemption.

## Redeploying

```bash
qa-engine deploy        # fetch + reset --hard origin/main -> pip -> npm ci -> playwright chromium (if missing)
                        #   -> pm2 start (first time) / restart -> probe -> pm2 save
qa-engine restart       # pm2 restart + probe, no code change
qa-engine status        # HEAD, pm2 state, local + public probe, cert days
qa-engine logs          # tail pm2 logs (args pass through, e.g. -n 100)
qa-engine backup        # tar data/qa.db + screenshots into data/backups/
```

`redeploy` still works as an alias of `deploy`. `bin/qa-engine` is the merged
lab980 app-CLI template with this site's steps folded in, so it behaves like
every other `<stub>` on the box:

- **Sync is `git fetch` + `git reset --hard origin/main`**, not a pull. A
  tracked file edited on the droplet is destroyed silently on the next
  deploy — fix it in the repo. `.env`, `.venv/`, `node_modules/` and `data/`
  are gitignored and survive.
- **First start comes from `ecosystem.config.cjs`** (`pm2 start
  ecosystem.config.cjs --only qa-engine`) when nothing named `qa-engine` is
  registered; every later deploy is `pm2 restart qa-engine`. The ecosystem
  entry is the registration: `.venv/bin/gunicorn` with no interpreter, the
  gthread / `--timeout 0` args bound to `127.0.0.1:8044`, fork mode,
  `max_restarts: 10`, logs in `data/pm2-*.log`.
- **Every pm2 call runs from a scrubbed environment** — `env -i` plus `PATH`,
  `HOME`, `LANG`, `PM2_HOME`/`TERM` if set, and `PORT=8044`; never
  `--update-env`. pm2 copies the environment of the `pm2 start` call into the
  process and into `~/.pm2/dump.pm2`, so nothing exported in the shell that
  ran `deploy` can reach the process or the dump.
- **`deploy` fails, and saves nothing, when `127.0.0.1:8044` does not answer
  HTTP** (any status code counts; up to `QA_ENGINE_PROBE_TRIES`, default 10,
  tries a second apart). `pm2 save` runs only after that and only when every
  registered pm2 process is `online` — otherwise it warns and leaves the
  previous dump alone.
- Must run as root with root's `HOME` (`sudo -i` / `su -`), because the pm2
  daemon and dump are root's.

Overrides: `QA_ENGINE_FQDN`, `QA_ENGINE_BRANCH` (default `main`),
`QA_ENGINE_PORT` (default `8044`; the probe port — gunicorn's bind lives in
`ecosystem.config.cjs`), `QA_ENGINE_PROBE_TRIES` (default `10`).

## Environment / config

Everything tunable lives in `config.py`, overridable via the environment.
`config.py` loads **`/var/www/qa-engine/.env`** itself at import time (plain
`KEY=value` lines, `#` comments, optional `export `, single or double quotes;
values are never expanded or executed), so pm2-managed and hand runs read the
same file. A variable already set in the process environment wins over the
file. `QA_ENV_FILE=/path` points the loader elsewhere (tests use this).

- `ANTHROPIC_API_KEY` — **lives only in `/var/www/qa-engine/.env`, mode 600.**
  It is not in `/etc/environment` and pm2 does not inherit it from a login
  shell (both were closed off 2026-09-05); each app's own `.env` is the only
  copy of any key it uses. Write it with an editor, then `chmod 600 .env` and
  `qa-engine restart` (or `qa-engine redeploy`) — the file is read at process
  start, so an edit does nothing until the restart. `.env` is gitignored and
  survives a redeploy.

  **Without it the app runs in mock-model mode and keeps serving**: Tier 0
  (axe/security/Lighthouse), crawling, digests, the cache, and SSE all work
  for real; the Haiku/Sonnet tiers return canned results. Good for a smoke
  test, not for real reviews. It says so in two places — `GET /healthz`
  returns `{"status": "ok", "mock_models": true}` (`false` once the key is
  read), and every deep-review finding's text starts with `[mock] deep
  review: no findings (mock mode, no API key configured).` There is no banner
  in the UI, so check `/healthz` after any restart:

  ```bash
  curl -s https://qa-engine.lab980.com/healthz    # expect "mock_models": false
  ```
- `PORT` — local bind port (8044).
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
  upgrade, `qa-engine deploy` re-runs `npm ci` and re-checks the Playwright
  chromium install location (a playwright version bump moves it, so the check
  installs the new one).
