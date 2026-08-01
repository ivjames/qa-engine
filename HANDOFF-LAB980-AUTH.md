# Handoff: lab980-wide unauthenticated-surface sweep

For a session connected to the lab980 droplet and/or all its repos. Written
2026-08-01 from the session that added qa-engine's run history and found the
vulnerability class below. Work through this top to bottom; it is self-contained.

## The vulnerability class

Operator-only web apps on the lab980 droplet relying on **security through
obscurity** (an unadvertised subdomain) with **no authentication at any
layer**. The obscurity is void: every TLS cert issued for a subdomain is
published in Certificate Transparency logs (check crt.sh for `%.lab980.com`),
so scanners learn each hostname at cert-issuance time. The exposure that
matters is what an anonymous visitor can *do*, typically some mix of:

1. **Spend money** — endpoints that trigger AI-model calls (Anthropic/OpenAI
   keys live on this droplet in `/etc/environment`, `/etc/aa-admin.env`, and
   per-app `.env` files).
2. **Aim infrastructure at third parties** — crawlers/scanners accepting an
   arbitrary target URL make the droplet someone else's attack tool.
3. **Read operator data** — findings/reports/history pages that amount to a
   published vulnerability report for our own sites.
4. **Destroy state** — unauthenticated delete/reset endpoints.

## The canonical fix (already documented for qa-engine)

Vhost-level nginx basic auth. No app-code changes. See the "Locking it down"
section of qa-engine's `DEPLOY.md` on branch
`claude/qa-report-history-migration-69wfn4` for the exact recipe:
`htpasswd -c /etc/nginx/htpasswd-<app> ivan` → `auth_basic` +
`auth_basic_user_file` in the server block → `auth_basic off` exemption for
`/healthz` (duplicating the proxy_* lines) → `nginx -t && systemctl reload
nginx` → curl-verify 401 without and 200 with credentials.

Per-app decision rule:
- **Operator-only app** (no public audience): gate the whole vhost. Exempt
  only health checks and any endpoint an external machine must reach (see
  do-not-break list).
- **Mixed public/admin app**: do NOT vhost-gate. Verify the app-level auth on
  its admin surfaces is actually configured (an unset token env var can mean
  the gate 404s — good — or is wide open — check which).

## Known properties (starting inventory — ENUMERATE, don't trust this list)

| Property | Port | Repo | State as of this handoff |
|---|---|---|---|
| qa-engine.lab980.com | 8044 | ivjames/qa-engine | **Vulnerable until the nginx fix is applied on the droplet.** Docs + rationale are on branch `claude/qa-report-history-migration-69wfn4` (merge/deploy it, then apply the DEPLOY.md "Locking it down" steps). Exposes `POST /api/run/page|flow` (spends Anthropic budget, crawls arbitrary URLs), `/runs` (findings history + import), `POST /runs/<id>/delete`, `/screenshots/…`. |
| artificialatheist.com | 8060 | ivjames/artificial-atheist | Public site — do NOT vhost-gate. Admin surfaces (`/review/pipeline`, `/review/prophecy`, `/review/adversary`, `/review/qa`) use app-level `ADMIN_TOKEN` + `aa_admin` cookie and 404 when the token is unset. **Verify** `ADMIN_TOKEN` is set and non-trivial in `/etc/aa-admin.env` / app env, and spot-check one `/review/*` URL unauthenticated (expect a login card or 404, never content). |
| tools/admin (inside artificial-atheist repo) | separate node proc | ivjames/artificial-atheist | Old standalone admin dashboard, basic-auth at `/admin/`. Check whether it is currently running (`pm2 list`) and, if so, that its basic-auth credentials are real (not a default/empty pair). If it isn't needed, stop it rather than gate it. |
| everything else | ? | ? | Unknown to the authoring session — enumerate. |

## Sweep procedure

1. **Enumerate the real property list** (droplet): `ls /etc/nginx/sites-enabled/`,
   `pm2 list`, `ls /var/www/`, and `ss -tlnp | grep 127.0.0.1` for locally
   bound apps someone may have proxied. Cross-check against CT logs
   (crt.sh `%.lab980.com` and any other owned domains) — a cert with no
   surviving vhost is fine; a vhost missing from your mental inventory is the
   interesting case.
2. **Classify each vhost**: public site vs operator tool, using the decision
   rule above. Read the proxied app's routes if unsure (look for run/trigger/
   delete/import/admin endpoints and for AI-SDK usage = spend).
3. **Test before fixing** (record results): unauthenticated `curl` against the
   root, one state-changing endpoint, and one data-reading endpoint per app.
4. **Apply the fix** per the qa-engine recipe; one `htpasswd-<app>` file per
   app (shared passwords across tools are acceptable here, separate files keep
   revocation per-app).
5. **Verify after fixing**: 401 unauthenticated on gated paths; 200 on
   exemptions; the app's own UI still works logged in (SSE streams, uploads);
   the do-not-break list below still functions.
6. **Report**: per property — what was exposed, what was applied, before/after
   curl evidence. Commit any doc updates to each repo's DEPLOY/CLAUDE notes so
   the next provision doesn't regress it (provision-site writes vhosts; if the
   sweep edits vhosts by hand, note in each repo that re-provisioning drops
   the auth block and it must be re-applied).

## Do-not-break list (exemptions that must stay reachable WITHOUT basic auth)

- **artificialatheist.com Stripe webhook** — external POST from Stripe,
  trailing-slash URL form (`/api/...:/`). Authenticated by Stripe signature,
  not by nginx. Basic auth here silently breaks payments.
- **artificialatheist.com deploy webhook** — git push → webhook → `deploy.sh`.
  Breaks deploys if gated; it authenticates via its own secret.
- **Health checks** (`/healthz` and equivalents) — used by probes; exempt with
  `auth_basic off`.
- Anything else discovered that is called by a machine (Buffer callbacks, cron
  fetchers): exempt the specific path and confirm it has its own secret or
  signature check; if it has none, that's a finding of its own.

## Explicitly out of scope

- Building app-level auth into qa-engine (decided against; nginx owns it).
- Changing artificial-atheist's existing token-auth scheme.
- Anything that requires new DNS or new certs.
