# Self-hosted fork: must-have patches and upstream sync

This repo (`<github-user>/posthog`) tracks `PostHog/posthog` but carries a permanent
set of local patches that make sense for a private, self-hosted instance and
would never be accepted upstream (they remove cloud billing/licensing gates,
change which AI provider is used, and cut outbound calls to PostHog's own
services). This document is the single source of truth for what those patches
are and how they survive an upstream sync.

## Branch layout

`master` is the only branch.
It is `upstream/master` (`PostHog/posthog`) plus the fork patches in the table below, and it is the branch that gets built and deployed.
The images are built by hand from `master` and pushed to Docker Hub as `docker.io/<dockerhub-user>/posthog-selfhost`.
Sync by merging `upstream/master` into `master` (see "Syncing with upstream").
A conflict is resolved once per sync, and the patch table says which behavior to keep.

## Must-have patches

Each entry is mandatory: it must survive every sync with a new
`upstream/master`. When a merge conflict touches one of these files, re-read
the "Why" line before resolving it — the goal is to keep the *behavior*
described, not necessarily the exact diff.

| # | Area | Why | Status |
|---|------|-----|--------|
| 1 | Remove `is_cloud` / billing / license gating (backend + frontend) | Self-hosted instance; every `AvailableFeature` should be unlocked and billing/license checks should not gate functionality. See the restriction table gathered during investigation for the full file list (`posthog/models/organization.py`, `posthog/utils.py`, `posthog/api/project.py`, `ee/billing/billing_manager.py`, `ee/api/billing.py`, `ee/api/license.py`, `posthog/tasks/sync_billing.py`, `posthog/tasks/usage_report.py`, `ee/tasks/send_license_usage.py`, `ee/api/subscription.py`, `products/tasks/backend/access.py`, `products/legal_documents/backend/logic/__init__.py`, `products/data_warehouse/.../data_warehouse.py`, `products/warehouse_sources/.../row_tracking.py`, `ee/partners/stripe/api/provisioning/*`, `ee/support_sidebar_max/max_search_tool.py`, `frontend/src/types.ts`, `frontend/src/scenes/userLogic.ts`, `frontend/src/lib/logic/featureFlagLogic.ts`). | Applied — `ee/partners/stripe/api/provisioning/*` (Stripe marketplace provisioning) and `ee/support_sidebar_max/max_search_tool.py` (Max support search) still make outbound calls with no self-hosted gate; both are patch #3 (no outbound calls) follow-ups, not feature gates. |
| 2 | PostHog AI → OpenRouter only | All LLM calls from PostHog AI (`ee/hogai/...` and the legacy `ee/support_sidebar_max`) must route through OpenRouter, not directly to Anthropic. | **Satisfied by deploy config, no code diff** — see "PostHog AI provider routing" below. |
| 3 | No outbound calls to PostHog's own services | Nothing in this deployment should call `*.posthog.com`, `*.i.posthog.com`, `posthogstatus.com`, or the license/billing/usage-report endpoints. | Applied — `posthog/settings/base_variables.py` (`OPT_OUT_CAPTURE` hardcoded `True`), `ee/support_sidebar_max/max_search_tool.py` (sitemap fetch skipped), `ee/partners/stripe/api/provisioning/{billing,services_catalog}.py` and every remaining outbound method on `ee/billing/billing_manager.py` (gated behind `is_cloud()`), `useAdblockDetection.ts` (probe skipped). `incidentStatusLogic.tsx`'s status-page poll and `region_proxy.py` were already self-hosted-safe. |
| 4 | Whole-app IP allowlist | The app itself (root, login, dashboard, `/admin`, API — everything not explicitly public) must reject requests from IPs outside a configured allowlist. Only event ingestion (capture, replay, flags, surveys, webhooks, remote-config, objectstorage, livestream) stays open to every IP, since tracked websites' visitors need to reach it from anywhere. | Applied — see "Whole-app IP allowlist" below. |
| 5 | Anthropic token-counting fallback | Max/PostHog AI's context-compaction logic calls Anthropic's `count_tokens` beta endpoint directly on the `ChatAnthropic` model, which OpenRouter's Anthropic-compatible endpoint (patch #2) doesn't implement — it 404s and, uncaught, fails the entire chat turn silently ("unable to respond right now"). | Applied — `ee/hogai/core/agent_modes/compaction_manager.py`. |
| 6 | Lower extended-thinking budget | Upstream runs every Max message through extended thinking with a 10240-token budget regardless of complexity, adding latency to even trivial messages. Not an outage, just materially slower than necessary on a resource-constrained self-hosted box. | Applied — `ee/hogai/core/agent_modes/executables.py` (`AgentExecutable.THINKING_CONFIG`), lowered to 3072. `research_agent/executables.py`'s separate 4096 budget was left alone. |
| 7 | Skip automatic memory-fact extraction | Confirmed via OpenRouter's own logs (Sep 17): a single Max turn fires 6 sequential model calls (title gen, memory collection, 3x root-agent tool-use iterations, plus a taxonomy/query call), one measured at 19.4s alone. `MemoryCollectorNode` makes an unconditional extra `gpt-4.1` call on *every* message to extract facts about the user's product for later recall — a background nice-to-have, not something the user is waiting to see, but a full sequential round trip on every turn regardless. This is a deliberate capability trade: Max stops automatically learning facts about the product from casual conversation. | Applied — `ee/hogai/chat_agent/memory/nodes.py` (`MemoryCollectorNode.arun` returns early after the explicit `/remember` command check, which still works). |
| 8 | Fix 404 on conversation retrieve right after creation | `ConversationViewSet.safely_get_queryset` required `title__isnull=False` for both `list` and `retrieve`. A brand-new conversation has no title until `TitleGeneratorNode` sets it asynchronously after the first LLM call, so the frontend's retrieve-by-id GET right after opening a new chat 404s until the title lands and a retry succeeds — visible as a 404 in the browser Network tab on every new chat. | Applied — `ee/api/conversation.py` (`safely_get_queryset` only applies the title filter for `list`; `retrieve` already has the exact conversation ID and doesn't need it). Verified live: fresh conversation's repeated retrieve GETs all returned 200 through a full real tool-use turn. |
| 9 | AI subscriptions not Cloud-only (frontend) | Upstream added two frontend gates that hide AI-prompt subscriptions unless `preflight.cloud` or debug (`getAiSubscriptionGate` in `products/subscriptions/frontend/components/Subscriptions/utils.tsx` and `aiSubscriptionsAvailable` in `subscriptionsSceneLogic.tsx`). They would undo the backend unlock in patch #1 (`_ai_create_gate_reason`). Still needs the `SUBSCRIPTION_AI_PROMPT` feature flag and the org's AI data processing consent. | Applied 2026-09-19 at the merge of upstream `4b1cbedf39a`. |
| 10 | OIDC reads identity claims from the ID token | PostHog's OIDC backend took `email` and `email_verified` only from the provider's userinfo response. ADFS serves `sub` alone from userinfo and carries every other claim in the ID token, and it never sends `email_verified` at all, so every ADFS login failed with "OIDC requires a verified email address from the identity provider". A relying party also picks its own outgoing claim type per attribute, so the same value arrives as an OIDC short name, a SAML claim-type URI, or a hand-typed label. | Applied — `posthog/api/oidc.py`. Claims resolve from userinfo first and the ID token second, across a candidate list per canonical name (`CLAIM_CANDIDATES`). An absent `email_verified` is accepted because it is OPTIONAL in OIDC Core; an explicit false still rejects. `upn` backs `email`, and `unique_name` is reformatted into a display name so a signup is not refused for an empty name. Verified against a real ADFS token on 2026-09-20. |
| 11 | Close public organization creation | `SignupViewset` and `SocialSignupViewset` are the only paths that create an authenticated user without an invite or an identity provider, and this instance answers on the public internet. Patch #1 had made `get_can_create_org` return `True` unconditionally while removing license gating, which left registration open to anyone who reached the login page. | Applied — `posthog/settings/web.py` (`ORG_CREATION_ENABLED`, default false) and `posthog/utils.py` (`get_can_create_org`). Staff keep the ability, so an administrator is never locked out. Members join through the identity provider or an invite. |

### Anthropic token-counting fallback (patch #5)

Found by actually testing Max in a real browser (`browser_batch`/Chrome extension) after
patches for the task-queue and personhog regressions above still left it responding with
"I'm unable to respond right now." The real error, from `root-temporal-django-worker-1`:

```
NotFoundError: Error code: 404 - {'error': {'message': 'Not Found', 'code': 404}}
  ee/hogai/core/agent_modes/compaction_manager.py: AnthropicConversationCompactionManager._get_token_count
  -> langchain_anthropic ChatAnthropic.get_num_tokens_from_messages
  -> anthropic.resources.beta.messages.messages.count_tokens
```

`get_num_tokens_from_messages` calls Anthropic's `/v1/messages/count_tokens` beta endpoint to
size the conversation window before deciding whether to compact it. OpenRouter's
Anthropic-Messages-API compatibility (patch #2) covers the main `/v1/messages` chat
completion endpoint but not this separate beta endpoint — it 404s, and the exception wasn't
caught anywhere in the call chain, so the whole chat-agent Temporal activity failed on every
single turn, network-layer-deep in a codepath with no self-hosted-specific handling.

Fixed by wrapping the real API call in `_get_token_count` with `except anthropic.APIStatusError`
(broad enough to catch any bad HTTP status from this specific call, not just 404) and falling
back to the same character-count-based estimate (`APPROXIMATE_TOKEN_LENGTH = 4` chars/token)
this class already uses for short conversations. Token counting becomes approximate instead of
exact — it only affects *when* compaction triggers, not the correctness of any actual response.

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
`ADMIN_ALLOWED_IPS=<your-ip>`). Updating the allowlist is an `.env` edit + a proxy
container restart — no rebuild, no code change.

Validated directly against a real `caddy` binary (both syntax — `caddy validate` — and
behavior): a loopback request to `/` and to `/admin/` both pass through (200), and — in the
first version of this patch — a non-loopback request (including one with a forged
`X-Forwarded-For: <your-ip>` header) got `403` while `/e/` stayed unaffected. The
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

**This is config, not code, so it is invisible to `git diff`/merges and to
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

**A second, related gap in the same merge**: fixing the task queue got Max as far as
responding with "I'm unable to respond right now" instead of pure silence — progress, but
still broken. The actual failure: `RuntimeError: personhog client not configured` in
`posthog.personhog_client.client.personhog_call`, thrown from Max's `chat-agent` workflow
activity when it looks up group types for the project. `web` and `worker` both set
`PERSONHOG_ADDR: personhog-router:50052` and `PERSONHOG_ENABLED: 'true'` in
`docker-compose.hobby.yml`; `temporal-django-worker`'s service definition in the same file
never did — this looks like an upstream gap in the hobby compose file itself (affects any
hobby deployment running Max, not something specific to this fork), exposed only because
patch 1's task-queue fix let the workflow actually reach the personhog call. Fixed by adding
both vars to `temporal-django-worker`'s `environment:` block in
`docker-compose.override.yml`, alongside the task-queue `command:` override above.

Update this table (add rows, change "Status", link the commit) every time a
must-have patch is added, changed, or removed. Never delete a row silently —
if a patch is retired, say why.

## Deploy checklist

Everything above is committed to `master` but is **not live** until the branch is
built into an image and deployed. Before deploying:

1. Set `ADMIN_ALLOWED_IPS` in `/root/.env` (space-separated IPs/CIDRs, e.g.
   `ADMIN_ALLOWED_IPS=<your-ip>`). Without it, only `127.0.0.1`/`::1` can reach
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

## Known operational gap: capture's restart policy (found 2026-09-17)

`capture` (and several other Rust services) run an internal lifecycle monitor that
self-terminates — **exit code 0, a clean shutdown** — when a dependency stalls (Kafka,
Redis). Under this host's recurring memory pressure, `capture` and `replay-capture` both
self-terminated and stayed down for **21+ minutes with zero events ingested**, discovered
only by manually auditing container status. The reason it went unnoticed: their inherited
restart policy is `on-failure`, which only restarts on a *nonzero* exit — a clean
self-shutdown never triggers it.

Fixed via `docker-compose.override.yml` (untracked, server-only — same caveat as the other
override-based fixes above): `restart: always` on `capture`, `replay-capture`,
`capture-logs`, `cymbal`, `cymbal-resolution`, and `property-defs-rs` — the Rust services
most likely to share this lifecycle-monitor pattern.

**Update (2026-09-17, after the RAM upgrade below): the same gap hit a Node service too.**
The VM was resized from 15GB/8vCPU to 32GB/16vCPU (see below), which required a reboot.
`ingestion-sessionreplay` came back up racing Redis's own startup, hit `EAI_AGAIN` resolving
`redis7`, self-terminated cleanly (exit 0) same as `capture` did, and sat dead post-reboot for
the same `on-failure` reason. Extended `restart: always` to `ingestion-general`,
`ingestion-sessionreplay`, `ingestion-error-tracking`, `ingestion-logs`, and
`ingestion-traces` too — this class of bug isn't Rust-specific, it's "anything that treats a
dependency hiccup as fatal instead of retrying," which turns out to be most of the ingestion
fleet. `capture-logs`'s sibling services in that same family were already covered above.

**This is a symptom, not the disease.** The `capture`/`replay-capture` incident's root cause
was VM memory pressure; the `ingestion-sessionreplay` incident's root cause was reboot-time
service ordering (a one-time event, not recurring pressure) — different triggers, identical
failure mode (clean self-exit + `on-failure` never catches it). `restart: always` makes both
kinds of outage self-heal in seconds instead of requiring a human to notice, but it doesn't
stop the underlying stall/race from happening. Worth adding actual monitoring/alerting on
container restart counts if this keeps recurring, rather than relying on someone periodically
running `docker ps -a` by hand.

## VM resized: 15GB/8vCPU → 32GB/16vCPU (2026-09-17)

The RAM/CPU pressure documented throughout this file (swap thrashing during builds, the
AI-latency investigation, and both restart-policy incidents above) was real and load-bearing
enough that the operator resized the underlying VM. Confirmed post-resize:
`free -h` → 31Gi total, 15Gi available, **0B swap in use** (down from routinely
4-6GB of swap in active use on the old 15GB box). `nproc` → 16 (was 8).

This doesn't remove any of the fixes above — the restart-policy hardening and the
thinking-budget reduction are still correct and worth keeping regardless of how much
headroom the host has. It does mean the *frequency* of memory-pressure-triggered incidents
(like the `capture` self-shutdown) should drop sharply. If `capture`/`replay-capture`/etc.
still self-terminate regularly on the resized box, the cause has shifted from "not enough
RAM" to something else (a real memory leak, an actual Kafka/Redis problem, or undersized
per-service resource limits) and is worth investigating fresh rather than assuming it's the
same capacity issue.

## Syncing with upstream

The fork syncs by merging `upstream/master` into `master`.
It does not rebase, because a rebase would rewrite the published history.
[`bin/sync-upstream.sh`](../../bin/sync-upstream.sh) belongs to an older two-branch layout and is not used now.
Run every command in this section from the repo root on the Mac, with `master` checked out.

1. Get the real upstream tip.
   The local clone is shallow (`.git/shallow`), so `upstream/master` can stay stale after a fetch that reports success.

   ```bash
   git ls-remote upstream refs/heads/master
   ```

2. Fetch it explicitly, then check that the ref equals the SHA from step 1.
   The fetch takes several minutes and prints nothing while git indexes the pack.

   ```bash
   git fetch upstream master --no-tags
   ```

   ```bash
   git rev-parse upstream/master
   ```

3. Count the incoming commits.

   ```bash
   git rev-list --count master..upstream/master
   ```

4. List the files that upstream and our patches both changed.
   These are the likely conflicts.

   ```bash
   BASE=$(git merge-base master upstream/master); comm -12 <(git diff --name-only $BASE upstream/master | sort) <(git diff --name-only $BASE master | sort)
   ```

5. Scan the incoming code for new limits and outbound calls, and read the context of each hit.
   Repeat the scan for `*.ts` and `*.tsx` with `preflight.cloud|isCloud|hasAvailableFeature`.

   ```bash
   BASE=$(git merge-base master upstream/master); git diff $BASE upstream/master -U0 -- '*.py' | grep -E '^\+.*(is_cloud\(\)|AvailableFeature\.|is_team_limited|[a-z]+\.posthog\.com)'
   ```

6. Check whether upstream changed the compose files.
   The server keeps its own copies in `/root`, and they do not update themselves.
   If this prints anything, port the change to the VM by hand before deploying.

   ```bash
   git diff --stat $(git merge-base master upstream/master) upstream/master -- docker-compose.base.yml docker-compose.hobby.yml
   ```

7. Merge.

   ```bash
   git merge upstream/master --no-edit
   ```

8. Confirm that each patch survived.
   Every command must print a match.
   The patch table above lists all patched files.

   ```bash
   grep -n 'if self.action == "list"' ee/api/conversation.py; grep -n '"budget_tokens": 3072' ee/hogai/core/agent_modes/executables.py; grep -n 'except anthropic.APIStatusError' ee/hogai/core/agent_modes/compaction_manager.py; grep -n 'app-restricted' docker-compose.base.yml
   ```

9. Patch any new gate found in step 5, then commit.
   Add a row to the patch table and a line to the sync log below.

10. Push over SSH.
    The HTTPS token has no `workflow` scope, and GitHub rejects a push whose history changes workflow files.

    ```bash
    git push git@github.com:<github-user>/posthog.git master
    ```

## Building the image

The image is built on the Mac and pushed to Docker Hub.
The VM only pulls it.

1. Docker Desktop needs at least 8 CPUs and 12 GB of memory (Settings, Resources).
   Its 1 CPU and 1 GB default cannot build this image.

   ```bash
   docker info --format 'cpus={{.NCPU}} mem={{.MemTotal}}'
   ```

2. If the build fails with "no space left on device", free the build cache.

   ```bash
   docker builder prune -af
   ```

3. Build and push two tags.
   The dated tag is a rollback target, because `:selfhost` is overwritten on every build.
   Expect 30 to 60 minutes: the amd64 build runs under emulation on Apple Silicon.

   ```bash
   docker buildx build --builder posthog-amd64 --platform linux/amd64 -t docker.io/<dockerhub-user>/posthog-selfhost:selfhost -t docker.io/<dockerhub-user>/posthog-selfhost:sync-YYYYMMDD-SHORTSHA -f Dockerfile --push --progress=plain . > /tmp/build.log 2>&1
   ```

4. Check that both tags point to the same digest.

   ```bash
   docker buildx imagetools inspect docker.io/<dockerhub-user>/posthog-selfhost:selfhost | grep Digest
   ```

This builds only the main image (`web`, `worker`, both Temporal workers).
The seven Node services (`plugins`, `ingestion-*`, `recording-api`) run a separate image, `docker.io/<dockerhub-user>/posthog-selfhost-node:selfhost`.
It was built on 2026-03-31, this flow does not rebuild it, and how it was built is not recorded.

## Deploying to the VM

Connect to the VM as described in the private notes file (`docs/internal/self-hosted-private.md`, gitignored).
Every command below runs on the VM.

1. Tag the running image, so a rollback is one command.
   Use the date of the deploy.

   ```bash
   sudo docker tag docker.io/<dockerhub-user>/posthog-selfhost:selfhost docker.io/<dockerhub-user>/posthog-selfhost:rollback-YYYYMMDD
   ```

2. Optional: back up Postgres before a sync that brings migrations.
   The dump is about 30 MB. Delete it when the deploy is confirmed.

   ```bash
   sudo mkdir -p /data/backups && sudo sh -c 'docker exec root-db-1 pg_dump -U posthog -Fc posthog > /data/backups/pre-deploy.dump'
   ```

3. Pull the new image (about 3 GB).

   ```bash
   cd /root && sudo docker compose -f docker-compose.yml -f docker-compose.override.yml pull web
   ```

4. Recreate the services that use it.
   Compose restarts only the changed services, and `capture` keeps running.
   `web` takes about 10 minutes to start, because migrations run at boot, and Caddy answers 502 until then.

   ```bash
   cd /root && sudo docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
   ```

5. Poll until `web` answers `HTTP/1.1 302`.

   ```bash
   sudo docker exec root-proxy-1 wget -q -O /dev/null -S http://web:8000/ 2>&1 | head -1
   ```

6. Check that the migrations applied.
   The last row must be a new migration.

   ```bash
   sudo docker exec root-db-1 psql -U posthog -d posthog -Atc "select app, name from django_migrations order by id desc limit 1"
   ```

7. Look for unhealthy containers.
   No output means all are healthy.

   ```bash
   sudo docker ps --format '{{.Names}}\t{{.Status}}' | grep -i -E 'unhealthy|restarting|starting'
   ```

8. Open the site and send one PostHog AI chat.
   Then run the checks in "Deploy checklist" that apply to the change.

### Rolling back

Re-tag the rollback image as `:selfhost`, then run step 4 again.
Migrations only move forward, so a rollback across a schema change also needs the Postgres dump restored.

```bash
sudo docker tag docker.io/<dockerhub-user>/posthog-selfhost:rollback-YYYYMMDD docker.io/<dockerhub-user>/posthog-selfhost:selfhost
```

When the deploy is confirmed, delete the dump and prune old images.

```bash
sudo rm -f /data/backups/pre-deploy.dump && sudo docker image prune -f
```

### Things that went wrong before

- **A stale `upstream/master` ref.** A fetch reported success and changed nothing. Always compare with `git ls-remote` (sync step 1).
- **The HTTPS push was rejected.** The token lacks the `workflow` scope. Push over SSH (sync step 10).
- **Docker Desktop was set to 1 CPU and 1 GB.** The build cannot run at that size (build step 1).
- **A `.env` edit restarted `web`.** Compose also recreates services that depend on a changed one, so batch `.env` edits. To restart only the proxy, add `--no-deps`.
- **Server-only files are not in git.** `/root/.env` and `/root/docker-compose.override.yml` hold the allowlist, email settings and worker sizing. Back them up before editing.
- **The allowlist lives in `/root/.env`.** Change `ADMIN_ALLOWED_IPS` (keep it quoted), then recreate only the proxy.

  ```bash
  cd /root && sudo docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-deps --force-recreate proxy
  ```

## Operational notes

Host-specific details (domain, addresses, SSH access, firewall, mail server) are kept in `docs/internal/self-hosted-private.md`, which is gitignored.
Do not write hostnames, IP addresses, ports or account names into tracked files.

- **The network config must match the NIC's MAC address.** After a NIC change, a stale MAC makes netplan skip the config without an error. The VM then falls back to the provider's DHCP DNS, which may not resolve names, and Caddy, the AI calls and outbound mail all fail. Check `resolvectl status` after any NIC change.
- **The VM's outbound IP is its public floating IP.** Anything that allowlists this server, such as a mail relay or a partner API, must allow that address.
- **Email is disabled** (`EMAIL_ENABLED=false` in `.env` and the instance setting). With email enabled, PostHog requires email verification at login and sends a new-device notification synchronously. An unreachable SMTP host then blocks each login for about two minutes before it fails. Turn email on only after the mail server allows the VM's public IP.
- **Never publish a container port on `0.0.0.0` unless it should be public.** The host firewall does not filter ports that Docker publishes. Bind internal ports to `127.0.0.1`.
- **Old data volumes are kept on purpose.** `root_clickhouse-data`, `root_postgres-data`, `root_redis7-data` and `root_objectstorage` (about 13.5 GB) are named Docker volumes that no container mounts, because the override uses bind mounts under `/var/lib/posthog`. The ClickHouse one is larger than the live data and was written until Sep 16, so it may hold events the live copy lacks. Compare before deleting. If the override's bind mounts are ever removed, compose would silently switch to these stale volumes.
- **Web concurrency is set by `GRANIAN_WORKERS`, not `WEB_CONCURRENCY`.** Under ASGI a sync Django view holds its whole worker until it finishes (`bin/docker-server`), so the worker count is the maximum number of concurrent requests. The upstream default of 4 made every page (30+ parallel API calls) queue while 16 CPUs sat idle. The server override sets `GRANIAN_WORKERS=14` (about 1.2 GB each) and `GRANIAN_WORKERS_MAX_RSS=2048` so Granian respawns any worker that creeps past 2 GB. `WEB_CONCURRENCY` only controls Celery (`bin/docker-worker-celery`); it is set to 4 on the `worker` service (upstream default is one process per CPU) to pay for the extra web memory. `vm.swappiness=10` (`/etc/sysctl.d/99-tuning.conf`) keeps idle-but-needed pages such as Redpanda's out of swap.
- **Measured capacity (same 5-endpoint mix, generator in another container).** 10 workers plateaued at about 27 requests per second; 14 workers reach about 33. At 48 concurrent requests the p90 fell from 4.3 s to 3.0 s; below about 12 concurrent requests nothing changed. Postgres is now the next limit (about 3 CPU cores at saturation, roughly 95 ms of Postgres CPU per request).
- **Every request opens a new Postgres connection.** `CONN_MAX_AGE` is hard-coded to 0 in `posthog/settings/data_stores.py` (two places) and costs about 18 ms per request against 0.12 ms for a query on an open connection. Making it an env setting is the obvious next step, but the async AI code paths run ORM calls in pool threads that would each keep a connection. If tried, also set Postgres `idle_session_timeout` and `max_connections`, and turn on `CONN_HEALTH_CHECKS`.
- **Caddy compresses HTML and JSON only** (`encode zstd gzip` in `docker-compose.base.yml`, minimum 1 KB). Streaming `text/event-stream` responses, such as PostHog AI chat, are not compressed on purpose, so they are not buffered.
- **Hashed static files are cached for a year.** Caddy sets `Cache-Control: public, max-age=31536000, immutable` on `/static/*-<8 hash chars>.<ext>` (JS, CSS, source maps, fonts). Unhashed files such as `array.js` and `Inter.woff` keep the app's 1-hour cache.
- **Two Temporal workers run.** `temporal-django-worker` serves only `max-ai-task-queue` (AI chat). `temporal-django-worker-general` serves `general-purpose-task-queue` (exports, subscriptions, data imports, other workflows), which had no worker until 2026-09-20. It is defined in the server-only `docker-compose.override.yml` with `extends` plus a shared environment anchor, so it reuses the AI worker's settings. Check both with `tctl taskqueue describe --taskqueue <name>` (an empty poller list means nobody serves it).
- **Materialized columns (dmat) do not work on this instance.** Tried 2026-09-20 and reverted. PostHog blocks `$`-prefixed system properties from materialization (`$host`, `$pathname`, `$current_url` are the most-read properties in slow queries). For custom properties the backfill fails with `Table posthog.dmat_slot_assignments does not exist`: that table and its dictionary are defined only in the newer HCL schema files (`posthog/clickhouse/hcl/`) and no numbered ClickHouse migration creates them on a hobby install. Other HCL-only objects may be missing as well. The weekly schedule function `create_or_update_weekly_dmat_backfill_schedule` is also never called anywhere in the repo. Diff the HCL schema against the live ClickHouse before retrying.
- **`web` takes about 10 minutes to start** after a recreate or reboot (migrations run at boot). Expect 502 from Caddy until it is up.

## Sync log

- **2026-09-19, merged upstream `4b1cbedf39a` (563 commits) into the fork.** Done as a merge, not a rebase. No conflicts. Reviewed the incoming diff for new gates: only the two frontend AI-subscription gates needed a patch (row 9). Checked and left alone: `_toolbar_entitlements` (already unlocked for non-Cloud), `is_team_over_ai_credit_budget` (reads a quota cache that self-hosted never fills), `LOGS_RETENTION_30D` gating (covered by the all-features-unlocked patch #1), and the new `posthog.llm.gateway_client` (only calls the URL configured in `LLM_GATEWAY_URL`, so nothing goes to a PostHog-owned host).
- **Gotcha: this local clone is shallow** (`.git/shallow`). `git fetch upstream` can report success while leaving `upstream/master` stale, and `git rev-list --count` against it is meaningless. Compare with `git ls-remote upstream refs/heads/master` before trusting the ref, and fetch `upstream master` explicitly.
