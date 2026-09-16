# Self-hosted fork: must-have patches and upstream sync

This repo (`moramin/posthog`) tracks `PostHog/posthog` but carries a permanent
set of local patches that make sense for a private, self-hosted instance and
would never be accepted upstream (they remove cloud billing/licensing gates,
change which AI provider is used, and cut outbound calls to PostHog's own
services). This document is the single source of truth for what those patches
are and how they survive an upstream sync.

## Branch layout

- `master` — a clean mirror of `upstream/master` (`PostHog/posthog`). No local
  commits ever land here. It exists so `git diff master upstream/master` is
  always empty and so the sync script has a known-good fast-forward target.
- `self-hosted` — `master` plus every patch in the table below, rebased onto
  `master` each time we sync. **This is the branch that gets built and
  deployed.** The production images (`ghcr.io/moramin/posthog-selfhost*`) are
  built from this branch, never from `master`.

Do not commit fork-only changes to `master`. Do not let `self-hosted` merge
commits pile up — it should always be a linear rebase on top of `master`, so
a conflict only ever has to be resolved once per patch, not once per merge.

## Must-have patches

Each entry is mandatory: it must survive every rebase onto a new
`upstream/master`. When a rebase conflict touches one of these files, re-read
the "Why" line before resolving it — the goal is to keep the *behavior*
described, not necessarily the exact diff.

| # | Area | Why | Status |
|---|------|-----|--------|
| 1 | Remove `is_cloud` / billing / license gating (backend + frontend) | Self-hosted instance; every `AvailableFeature` should be unlocked and billing/license checks should not gate functionality. See the restriction table gathered during investigation for the full file list (`posthog/models/organization.py`, `posthog/utils.py`, `posthog/api/project.py`, `ee/billing/billing_manager.py`, `ee/api/billing.py`, `ee/api/license.py`, `posthog/tasks/sync_billing.py`, `posthog/tasks/usage_report.py`, `ee/tasks/send_license_usage.py`, `ee/api/subscription.py`, `products/tasks/backend/access.py`, `products/legal_documents/backend/logic/__init__.py`, `products/data_warehouse/.../data_warehouse.py`, `products/warehouse_sources/.../row_tracking.py`, `ee/partners/stripe/api/provisioning/*`, `ee/support_sidebar_max/max_search_tool.py`, `frontend/src/types.ts`, `frontend/src/scenes/userLogic.ts`, `frontend/src/lib/logic/featureFlagLogic.ts`). | Applied — `ee/partners/stripe/api/provisioning/*` (Stripe marketplace provisioning) and `ee/support_sidebar_max/max_search_tool.py` (Max support search) still make outbound calls with no self-hosted gate; both are patch #3 (no outbound calls) follow-ups, not feature gates. |
| 2 | PostHog AI → OpenRouter only | All LLM calls from PostHog AI (`ee/hogai/...` and the legacy `ee/support_sidebar_max`) must route through OpenRouter, not directly to Anthropic. | **Satisfied by deploy config, no code diff** — see "PostHog AI provider routing" below. |
| 3 | No outbound calls to PostHog's own services | Nothing in this deployment should call `*.posthog.com`, `*.i.posthog.com`, `posthogstatus.com`, or the license/billing/usage-report endpoints. | Applied — `posthog/settings/base_variables.py` (`OPT_OUT_CAPTURE` hardcoded `True`), `ee/support_sidebar_max/max_search_tool.py` (sitemap fetch skipped), `ee/partners/stripe/api/provisioning/{billing,services_catalog}.py` and every remaining outbound method on `ee/billing/billing_manager.py` (gated behind `is_cloud()`), `useAdblockDetection.ts` (probe skipped). `incidentStatusLogic.tsx`'s status-page poll and `region_proxy.py` were already self-hosted-safe. |
| 4 | Whole-app IP allowlist | The app itself (root, login, dashboard, `/admin`, API — everything not explicitly public) must reject requests from IPs outside a configured allowlist. Only event ingestion (capture, replay, flags, surveys, webhooks, remote-config, objectstorage, livestream) stays open to every IP, since tracked websites' visitors need to reach it from anywhere. | Applied — see "Whole-app IP allowlist" below. |

### Whole-app IP allowlist (patch #4)

Enforced at the Caddy edge (`docker-compose.base.yml`'s `CADDYFILE` template), not in Django,
because there's no CDN in front of this deployment — Caddy sees the real client TCP source IP
directly, with no `X-Forwarded-For` spoofing risk, and a rejected request never reaches a
Django worker.

**Scope correction (2026-09-17):** the first version of this patch only restricted `/admin*`
(Django's low-level admin panel) and `/temporal-ui/*`, leaving the actual PostHog app — login,
dashboard, everything a normal user or attacker would hit — open to the whole internet. That
was a misreading of the original ask, caught only after deploying and testing: the operator
could still reach the login page from any IP. Fixed by restricting the Caddyfile's final
catch-all `handle {}` (which serves `web:8000`, i.e. the entire app) behind the same
`not remote_ip 127.0.0.1 ::1 ${ADMIN_ALLOWED_IPS:-}` check, via a new `@app-restricted`
matcher placed immediately before it. Because Caddy's `handle` blocks are mutually exclusive
and evaluated in file order, this only ever fires for requests that didn't already match one
of the explicit public matchers above it (`@capture`, `@replay-capture`, `@capture-ai`,
`@capture-logs`, `@flags`, `@surveys`, `@remote-config`, `@webhooks`, `@objectstorage`,
`@livestream`) — so the ingestion/public surface is unaffected. The separate `@temporal-ui`/
`@temporal-ui-restricted` pair still handles the Temporal UI route on its own; the old
`@admin-restricted` matcher (redundant now that the catch-all covers `/admin` too) was removed.

Localhost is always allowed (so local dev/in-container debugging never locks out); every other
caller needs its IP/CIDR listed in `ADMIN_ALLOWED_IPS` in `.env` (space-separated, e.g.
`ADMIN_ALLOWED_IPS=87.120.106.131`). Updating the allowlist is an `.env` edit + a proxy
container restart — no rebuild, no code change.

Validated directly against a real `caddy` binary (both syntax — `caddy validate` — and
behavior): a loopback request to `/` and to `/admin/` both pass through (200), and — in the
first version of this patch — a non-loopback request (including one with a forged
`X-Forwarded-For: 87.120.106.131` header) got `403` while `/e/` stayed unaffected. The
corrected version's non-loopback deny path was not re-verified against a real external IP in
this sandbox (no route to the test container's bridge IP); confirm it live post-deploy by
hitting `/` from a genuinely different network than the allowlisted one.

**`/temporal-ui/*` also needed `docker-compose.hobby.yml` changes**: `temporal` (port 7233,
raw gRPC) and `temporal-ui` (port 8081) were previously published to `0.0.0.0` — reachable
from the entire internet with zero authentication. This was discovered live on the deploy
host during this work, unrelated to the original ask, and is now fixed: both host port
publishes are removed (internal services already reach `temporal:7233` over the compose
network; `temporal-ui` is now only reachable through Caddy's IP-allowlisted `/temporal-ui/*`
route, proxied with `uri strip_prefix /temporal-ui`). CLI access to Temporal (`tctl` etc.) is
still available via `docker compose exec temporal-admin-tools`.

**Not verified**: whether Temporal UI's static assets/routing tolerate being mounted under a
`/temporal-ui` path prefix rather than at root — check this after deploying, since some SPAs
hardcode root-relative asset paths. If it doesn't render correctly, look for a Temporal UI
env var to set its public/base path (`TEMPORAL_UI_PUBLIC_PATH` or similar) rather than
reworking the Caddy route.

**Not covered by this patch**: `/root/docker-compose.override.yml` on the deploy host already
had `extra_hosts` entries pointing `billing.posthog.com`, `us.i.posthog.com`, and
`app.posthog.com` at `127.0.0.1` (DNS-level blocking, belt-and-suspenders on top of patch #3's
`is_cloud()` gates) plus `OPT_OUT_CAPTURE: "true"` — predates this work and isn't tracked in
this repo since it's a server-local override file, not a git-tracked compose file. Worth
migrating into a tracked file if this deployment is ever rebuilt from scratch.

### PostHog AI provider routing (patch #2)

Every Anthropic client in this codebase (`ee/hogai/llm.py`'s `MaxChatAnthropic`, via
`langchain_anthropic.ChatAnthropic`, and the legacy `ee/support_sidebar_max/views.py`'s
raw `anthropic.Anthropic(...)`) is constructed **without** an explicit `base_url`, so
both defer to the standard Anthropic SDK env vars. That means routing through
OpenRouter instead of Anthropic directly needs **no code change** — only the deploy
environment has to set:

```
ANTHROPIC_BASE_URL=https://openrouter.ai/api
ANTHROPIC_API_KEY=<an OpenRouter API key>
```

OpenRouter exposes an Anthropic-Messages-API-compatible endpoint at
`/api/v1/messages` (its "Anthropic Skin"), and bare Anthropic model IDs already
hardcoded in this repo (e.g. `claude-sonnet-4-6`) resolve correctly through it —
verified live against the deploy host on 2026-09-16, `claude-sonnet-4-6` returned
a 200 with `"model":"anthropic/claude-sonnet-4.6"` and normal usage/cost fields,
tool use and extended thinking (`betas`/`thinking` params) pass through as
Anthropic-format fields since it's the same wire protocol.

**This is config, not code, so it is invisible to `git diff`/rebases and to
anyone reading only the source.** If `.env` is ever regenerated (e.g. by
`hobby-installer`, or restoring a `.env.bak-*` that predates this) without
`ANTHROPIC_BASE_URL` set, PostHog AI silently falls back to calling
`api.anthropic.com` directly with whatever key is in `ANTHROPIC_API_KEY`. There
is no code guard against this — check `ANTHROPIC_BASE_URL` after any `.env`
change or reinstall.

**Also config, not code — the Temporal task queue Max runs on.** Max/PostHog AI chats
execute as Temporal workflows on `max-ai-task-queue`, not the image's default
`general-purpose-task-queue`. The pre-merge `docker-compose.yml` on the deploy host had
`temporal-django-worker` running `./bin/temporal-django-worker --task-queue max-ai-task-queue`
directly; the post-merge `docker-compose.hobby.yml` changed the command to
`/compose/temporal-django-worker`, a trivial wrapper (`./bin/temporal-django-worker`, no
flags) that silently falls back to the default queue — so the worker starts fine, looks
healthy, but never picks up Max's workflow tasks and the AI simply never responds. No error,
no crash, just silence. Fixed via a `command:` override on `temporal-django-worker` in
`/root/docker-compose.override.yml` (same untracked-file caveat as above — this needs
re-adding if the deployment is ever rebuilt from scratch). Confirmed via
`docker logs root-temporal-django-worker-1 | grep task_queue` showing
`"task_queue": "max-ai-task-queue"` after the fix.

Update this table (add rows, change "Status", link the commit) every time a
must-have patch is added, changed, or removed. Never delete a row silently —
if a patch is retired, say why.

## Deploy checklist

Everything above is committed to `self-hosted` but is **not live** until the branch is
built into an image and deployed. Before deploying:

1. Set `ADMIN_ALLOWED_IPS` in `/root/.env` (space-separated IPs/CIDRs, e.g.
   `ADMIN_ALLOWED_IPS=87.120.106.131`). Without it, only `127.0.0.1`/`::1` can reach
   `/admin*` and `/temporal-ui/*` — safe-by-default, but you'll lock yourself out too.

After deploying, verify in this order:

1. **Admin allowlist actually sees the real client IP.** This host runs Docker's
   userland-proxy (`docker-proxy`, confirmed running for ports 80/443/8081/7233 via
   `docker info` → `EnableUserlandProxy: true`), which is a userspace TCP relay — in
   principle it could present a NAT'd address to Caddy instead of the true client IP,
   which would silently break the whole allowlist (either locking everyone out, or —
   worse — letting everyone through). This was **not directly tested against the new
   code** before deploy; the only evidence it works is circumstantial (the existing
   `TRUST_ALL_PROXIES=true` on `web`/`capture` only makes sense if Caddy already sees
   real client IPs today). Test explicitly:
   - From your allowlisted IP: `curl -o /dev/null -w '%{http_code}\n' https://<domain>/admin/` → expect `200`/`302` (not `403`).
   - From a different network (phone on cellular, not wifi/VPN sharing the allowlisted IP): same request → expect `403`.
   - If the second check is *not* `403`, `remote_ip` is seeing a NAT'd address, not the real client — the allowlist is not enforcing anything, and needs a different approach (e.g. Caddy's `trusted_proxies`/`client_ip` with the docker-proxy's known address explicitly distrusted, or disabling `userland-proxy` in `/etc/docker/daemon.json` and using pure iptables DNAT).
2. **`/temporal-ui/*` renders correctly.** From an allowlisted IP, open `https://<domain>/temporal-ui/` — check the UI loads and its static assets/API calls resolve under the `/temporal-ui` prefix (see the "Not verified" note above; it may need a Temporal UI base-path env var if it doesn't).
3. **A billing-gated feature shows unlocked** — e.g. open an organization settings page that used to show an upgrade prompt and confirm it doesn't anymore.
4. **No outbound-call errors in logs** — `docker compose logs web worker | grep -i "billing.posthog.com\|us.i.posthog.com\|license.posthog.com"` should show nothing new (the existing `extra_hosts` DNS block would surface as connection-refused errors if something still tries).
5. Optional, not urgent: run `python manage.py sync_available_features` to instantly refresh `available_product_features` for organizations created before this patch, rather than waiting for the hourly Celery Beat task (`sync_all_organization_available_product_features`, confirmed running via `worker-beat`) to do it.

## Syncing with upstream

Run [`bin/sync-upstream.sh`](../../bin/sync-upstream.sh). It:

1. Fetches `upstream` (`PostHog/posthog`) and fast-forwards local `master` to
   `upstream/master` (refuses to run if `master` has diverged — that would
   mean a fork-only commit leaked onto `master`).
2. Pushes the fast-forwarded `master` to `origin` (your fork).
3. Rebases `self-hosted` onto the new `master`.
4. On a clean rebase, force-pushes `self-hosted` (`--force-with-lease`) and
   prints a summary.
5. On a conflict, stops and tells you which must-have patch (by file) needs
   attention — cross-reference the table above before resolving.

The script never merges and never pushes over someone else's newer work
without `--force-with-lease` protecting it. It does not build or deploy
images; that stays a manual/CI step after you've confirmed the rebased branch
is good (see "Publishing the self-hosted image" below, once that's set up).

### Manual steps if the script can't run

```bash
git fetch upstream master
git checkout master && git merge --ff-only upstream/master && git push origin master
git checkout self-hosted && git rebase master
# resolve conflicts, `git rebase --continue`
git push origin self-hosted --force-with-lease
```

## Local dev note

A sandboxed/agent checkout of this repo may be a shallow clone
(`git rev-parse --is-shallow-repository` → `true`), which makes
`git merge-base master upstream/master` unreliable. The sync script assumes a
full clone (as on the deploy server); unshallow first if running it from a
shallow checkout (`git fetch --unshallow`).
