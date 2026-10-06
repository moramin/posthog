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
| 1 | Remove `is_cloud` / billing / license gating (backend + frontend) | Self-hosted instance; every `AvailableFeature` should be unlocked and billing/license checks should not gate functionality. See the restriction table gathered during investigation for the full file list (`posthog/models/organization.py`, `posthog/utils.py`, `posthog/api/project.py`, `ee/billing/billing_manager.py`, `ee/api/billing.py`, `posthog/tasks/sync_billing.py`, `posthog/tasks/usage_report.py`, `ee/api/subscription.py`, `products/tasks/backend/access.py`, `products/legal_documents/backend/logic/__init__.py`, `products/data_warehouse/.../data_warehouse.py`, `products/warehouse_sources/.../row_tracking.py`, `ee/partners/stripe/api/provisioning/*`, `ee/support_sidebar_max/max_search_tool.py`, `frontend/src/types.ts`, `frontend/src/scenes/userLogic.ts`, `frontend/src/lib/logic/featureFlagLogic.ts`). | Applied — `ee/partners/stripe/api/provisioning/*` (Stripe marketplace provisioning) and `ee/support_sidebar_max/max_search_tool.py` (Max support search) still make outbound calls with no self-hosted gate; both are patch #3 (no outbound calls) follow-ups, not feature gates. |
| 2 | PostHog AI → OpenRouter only | All LLM calls from PostHog AI (`ee/hogai/...` and the legacy `ee/support_sidebar_max`) must route through OpenRouter, not directly to Anthropic. | **Satisfied by deploy config, no code diff** — see "PostHog AI provider routing" below. |
| 3 | No outbound calls to PostHog's own services | Nothing in this deployment should call `*.posthog.com`, `*.i.posthog.com`, `posthogstatus.com`, or the license/billing/usage-report endpoints. | Applied — `posthog/settings/base_variables.py` (`OPT_OUT_CAPTURE` hardcoded `True`), `ee/support_sidebar_max/max_search_tool.py` (sitemap fetch skipped), `ee/partners/stripe/api/provisioning/{billing,services_catalog}.py` and every remaining outbound method on `ee/billing/billing_manager.py` (gated behind `is_cloud()`), `useAdblockDetection.ts` (probe skipped). `incidentStatusLogic.tsx`'s status-page poll and `region_proxy.py` were already self-hosted-safe. |
| 4 | Whole-app IP allowlist | The app itself (root, login, dashboard, `/admin`, API — everything not explicitly public) must reject requests from IPs outside a configured allowlist. Only event ingestion (capture, replay, flags, surveys, webhooks, remote-config, objectstorage, livestream) stays open to every IP, since tracked websites' visitors need to reach it from anywhere. | Applied — see "Whole-app IP allowlist" below. |
| 5 | Anthropic token-counting fallback | Max/PostHog AI's context-compaction logic calls Anthropic's `count_tokens` beta endpoint directly on the `ChatAnthropic` model, which OpenRouter's Anthropic-compatible endpoint (patch #2) doesn't implement — it 404s and, uncaught, fails the entire chat turn silently ("unable to respond right now"). | Applied — `ee/hogai/core/agent_modes/compaction_manager.py`. |
| 6 | Max runs on DeepSeek V4 Pro (GA) with thinking disabled | Upstream runs every Max message through extended thinking on `claude-sonnet-4-6`. The agent makes many sequential rounds per answer, and thinking adds seconds to each round. Sonnet is also the dominant cost of a Max turn. | Applied — `ee/hogai/core/agent_modes/executables.py`: `AgentExecutable.THINKING_CONFIG` is `{"type": "disabled"}`, the interleaved-thinking beta is dropped, and the model is `deepseek/deepseek-v4-pro-0813`, the OpenRouter id that its Anthropic-compatible endpoint accepts. The disabled setting must be sent explicitly: DeepSeek reasons by default when the request leaves `thinking` out. The effort setting, the fine-grained-tool-streaming beta, prompt-cache markers, parallel tool calls and a tool-result round trip all work with it. Measured on one real question (the same one each time): this model took 35 s in 6 rounds with no tool errors; `deepseek/deepseek-v4-pro` (the preview) took 58 s in 12 rounds because it sent malformed tool arguments; Sonnet 5.5 took 29 s in 7 rounds. Going back to a Claude model needs `THINKING_CONFIG = None`: Sonnet 5.5 on OpenRouter rejects an explicit `disabled` with a 400 ("Reasoning is mandatory"). `research_agent/executables.py` keeps its own thinking budget and models. |
| 7 | Skip automatic memory-fact extraction | Confirmed via OpenRouter's own logs (Sep 17): a single Max turn fires 6 sequential model calls (title gen, memory collection, 3x root-agent tool-use iterations, plus a taxonomy/query call), one measured at 19.4s alone. `MemoryCollectorNode` makes an unconditional extra `gpt-4.1` call on *every* message to extract facts about the user's product for later recall — a background nice-to-have, not something the user is waiting to see, but a full sequential round trip on every turn regardless. This is a deliberate capability trade: Max stops automatically learning facts about the product from casual conversation. | Applied — `ee/hogai/chat_agent/memory/nodes.py` (`MemoryCollectorNode.arun` returns early after the explicit `/remember` command check, which still works). |
| 8 | Fix 404 on conversation retrieve right after creation | `ConversationViewSet.safely_get_queryset` required `title__isnull=False` for both `list` and `retrieve`. A brand-new conversation has no title until `TitleGeneratorNode` sets it asynchronously after the first LLM call, so the frontend's retrieve-by-id GET right after opening a new chat 404s until the title lands and a retry succeeds — visible as a 404 in the browser Network tab on every new chat. | Applied — `ee/api/conversation.py` (`safely_get_queryset` only applies the title filter for `list`; `retrieve` already has the exact conversation ID and doesn't need it). Verified live: fresh conversation's repeated retrieve GETs all returned 200 through a full real tool-use turn. |
| 9 | AI subscriptions not Cloud-only (frontend) | Upstream added two frontend gates that hide AI-prompt subscriptions unless `preflight.cloud` or debug (`getAiSubscriptionGate` in `products/subscriptions/frontend/components/Subscriptions/utils.tsx` and `aiSubscriptionsAvailable` in `subscriptionsSceneLogic.tsx`). They would undo the backend unlock in patch #1 (`_ai_create_gate_reason`). Still needs the `SUBSCRIPTION_AI_PROMPT` feature flag and the org's AI data processing consent. | Applied 2026-09-19 at the merge of upstream `4b1cbedf39a`. |
| 10 | AI usage is never rate limited | Self-hosted installs were throttled like free Cloud users: 10 chat messages a minute and 100 a day per user, research mode 3 a minute and 10 a day, hands-free voice 60 a day. Only a paying `customer_id` or a Cloud feature flag exempted a user. | Applied 2026-10-04 — `posthog/rate_limit.py` (`_AIThrottleBase.allow_request` returns `True` when not on Cloud, which covers chat, research, hands-free and the Max tools endpoint). |
| 11 | No Cloud-only blocks on AI features | `AI features are only available in PostHog Cloud` stopped the session replay AI client and the Temporal LLM endpoint on self-hosted. The calls still need `OPENAI_API_KEY` or a configured gateway. | Applied 2026-10-04 — `posthog/session_recordings/openai_client.py`, `posthog/temporal/ai_observability/llm_endpoint.py`. The upstream tests that assert the Cloud-only error now fail; the fork does not run them. |
| 12 | Replay vision is uncapped when billing never synced | Without billing data, an organization got the free-tier fallback credit cap (`REPLAY_VISION_MONTHLY_CREDIT_QUOTA`). | Applied 2026-10-04 — `products/replay_vision/backend/quota.py` (`credit_limit` is `None` when not on Cloud). |
| 13 | The hobby installer sends no telemetry | `bin/deploy-hobby` and the Go installer sent `magic_curl_install_start` and `magic_curl_install_complete`, including the install domain, to PostHog ingestion. The hobby compose file also defaulted `OPT_OUT_CAPTURE` to false. | Applied 2026-10-04 — `bin/deploy-hobby` (both calls removed), `bin/hobby-installer/core/telemetry.go` (no client, send is a no-op), `docker-compose.hobby.yml` (`OPT_OUT_CAPTURE` defaults to `true`). |
| 10 | OIDC reads identity claims from the ID token | PostHog's OIDC backend took `email` and `email_verified` only from the provider's userinfo response. ADFS serves `sub` alone from userinfo and carries every other claim in the ID token, and it never sends `email_verified` at all, so every ADFS login failed with "OIDC requires a verified email address from the identity provider". A relying party also picks its own outgoing claim type per attribute, so the same value arrives as an OIDC short name, a SAML claim-type URI, or a hand-typed label. | Applied — `posthog/api/oidc.py`. Claims resolve from userinfo first and the ID token second, across a candidate list per canonical name (`CLAIM_CANDIDATES`). An absent `email_verified` is accepted because it is OPTIONAL in OIDC Core; an explicit false still rejects. `upn` backs `email`, and `unique_name` is reformatted into a display name so a signup is not refused for an empty name. Verified against a real ADFS token on 2026-09-20. |
| 11 | Close public organization creation | `SignupViewset` and `SocialSignupViewset` are the only paths that create an authenticated user without an invite or an identity provider, and this instance answers on the public internet. Patch #1 had made `get_can_create_org` return `True` unconditionally while removing license gating, which left registration open to anyone who reached the login page. | Applied — `posthog/settings/web.py` (`ORG_CREATION_ENABLED`, default false) and `posthog/utils.py` (`get_can_create_org`). Staff keep the ability, so an administrator is never locked out. Members join through the identity provider or an invite. |
| 14 | Jev through OpenRouter, not TypeSafe | The System One features (the HogQL `jev()` and `decide()` functions, the signals judge, the replay vision rerank) need a System One server. TypeSafe's API is unreliable from this deployment: requests over about 2 KB stall. OpenRouter lists Jev only as the chat model `typesafe/jev-router`, which routes to general LLMs and offers no `/v1/systemone` route. | Applied — `posthog/llm/system_one_client.py` (`ChatCompletionsSystemOneClient`) and `posthog/hogql/transforms/prompt_jev.py`. Where `OPENAI_BASE_URL` points at OpenRouter and a key is set, the client sends the questions to the chat model with a strict JSON schema and builds System One answers from the returned probabilities. Choice and score answers are normalized to sum to 1, and a score is the expected index. The gateway still wins when configured, and OpenRouter wins over a TypeSafe key. These are chat-model estimates, not calibrated System One probabilities, so thresholds tuned against Jev can behave differently. Leave `TYPESAFE_API_KEY` empty. |
| 15 | No Cloud billing banner on warehouse sources | The new-source and source-list pages told self-hosted users they get 7 free days and a 100M-row cap. The backend never enforced a row limit on self-hosted (`will_hit_billing_limit` returns `False` off Cloud), so the text was only misleading. | Applied — `products/data_warehouse/frontend/shared/components/FreeHistoricalSyncsBanner.tsx` renders nothing unless `preflight.cloud` is true. Needs a frontend rebuild to take effect. |
| 16 | Today report Jev calls are not rate limited | Upstream added `JevBurstThrottle` (60 a minute) and `JevSustainedThrottle` (1,500 a day) per user on the Today briefing, candidates and excerpt endpoints. They are AI usage limits, so patch #10 applies. | Applied 2026-10-07 — `products/today/backend/presentation/views.py` (`_JevThrottleBase.allow_request` returns `True` when not on Cloud). The generic `BurstRateThrottle` and `SustainedRateThrottle` on the same endpoints stay. |

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

That expectation did not hold for long: see "Memory stall and the limits that followed" below.

## Memory stall and the limits that followed (2026-10-03)

### What happened

The host ran out of RAM with its 8 GB swap file full.
The load average read about 800 while the CPU sat idle: about 690 threads were blocked on disk, and iowait was 95%.
The site did not answer.
Redpanda stopped answering, `capture` logged `AllBrokersDown`, shut itself down, and Docker restarted it only after the host recovered.
The kernel killed nothing, because swap existed, so the host crawled instead of crashing.

Nothing bounded memory:

- 14 web workers took about 15 GB.
- ClickHouse was allowed 90% of host RAM (`max_server_memory_usage_to_ram_ratio` 0.9, about 28 GB).
- The Celery `worker` ran 5 processes at about 1 GB each.
- Redpanda, Elasticsearch, Postgres and the Node services took about 8 GB together.

The trigger is unknown.
The likely cause is a burst of heavy ClickHouse queries, but this is not confirmed.

### Recovery

1. Stop `web` first. It is the largest container and restarts cleanly. It freed about 14 GB at once and swap began to drain.
2. Set the limits below in the server-only `docker-compose.override.yml`.
3. Start `web` again. It needs 6 to 10 minutes.

### Limits now in force

| Area | Setting |
|---|---|
| `web` container | `mem_limit: 12g` |
| `worker` container | `mem_limit: 6g` |
| ClickHouse container | `mem_limit: 9g` (ClickHouse sizes its own cap from the cgroup limit) |
| ClickHouse per query | `max_memory_usage` 4 GB, in a `users.d` file mounted from the override |
| Web workers | `GRANIAN_WORKERS=8`, `GRANIAN_WORKERS_MAX_RSS=2048`, `GRANIAN_WORKERS_LIFETIME=43200` |
| Celery | `WEB_CONCURRENCY=2`, `CELERY_MAX_MEMORY_PER_CHILD=1500000` (KiB) |
| Protected from the OOM killer | `oom_score_adj: -500` on `kafka`, `db`, `capture`, `replay-capture` |
| `earlyoom` | Installed. It sends SIGTERM when available RAM is at or below 5% and free swap at or below 15%, and SIGKILL at 2.5% and 7.5%. It avoids sshd, dockerd, containerd and systemd. |

After the change the footprint was about 7.8 GB for `web` and 3.0 GB for `worker`.
Setting `oom_score_adj` in compose recreates the container, so applying it restarted Redpanda and Postgres once and paused ingestion for about a minute.

### Things learned

- **`memswap_limit` equal to `mem_limit` does not block swap on this host.** The cgroup file `memory.swap.max` stays `max`. The memory caps work (`memory.max` is set), but containers can still swap. Check the real value in `/sys/fs/cgroup/system.slice/docker-<id>.scope/memory.swap.max`.
- **`docker update --memory` applies live, but it does not persist.** The limits live in the override file, and the next `up -d` recreates the changed containers.
- **A container's memory figure includes page cache.** ClickHouse showed 6.3 GB, of which 4.5 GB was reclaimable file cache. Use anonymous memory plus swap from `memory.stat` for the real footprint.
- **Swap fills with cold pages of idle processes even when RAM is free.** This is harmless at zero memory pressure, but an idle worker stalls while its pages are read back.
- **More swap is not the fix.** Containers hold about 24 GB of 31 GB. A bigger swap lengthens a stall and delays `earlyoom`, which acts only when free swap is low. Compressed swap (zram) or more RAM would help; more disk swap would not.
- **Web workers creep slowly.** About 1.2 GB per worker after one day, against about 1.5 GB after 12 days on the old setup. The 2 GB RSS limit counts resident pages only, so a worker whose pages sit in swap is never recycled. The 12-hour lifetime covers that.
- **Do not cut web workers below 8.** Over 24 hours (9,077 requests) the requests in flight were 5 at the median, 12 at the 90th percentile, 20 at the 99th and 46 at the maximum.
- **Celery was idle.** The queues were empty, so two processes are enough.
- **ClickHouse queries rarely need more than 4 GB.** Over 7 days: 403,578 queries, 0.35 GB at the 99th percentile, about 127 queries between 4 and 8 GB, and 1,344 that already failed with a memory-limit error.
- **Several products are unused.** In 30 days: about 7 million events, no `$exception` events and no `$ai_*` events, and the logs and traces topics are empty. The ingestion services for those products, about 0.5 GB together, are candidates to stop.

### Checking for a stall

A load average far above the CPU count with an idle CPU means swap thrashing.

```bash
uptime; free -m; head -1 /proc/pressure/memory /proc/pressure/io
```

```bash
vmstat 1 3
```

In `vmstat`, a large `b` column (blocked) and a high `wa` (iowait) confirm it.
List the largest consumers with `ps -eo rss,etimes,comm --sort=-rss | head`, and check `earlyoom` with `journalctl -u earlyoom`.
Commands can hang during a stall, so run long ones detached and write the output to a file.

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
- **Web concurrency is set by `GRANIAN_WORKERS`, not `WEB_CONCURRENCY`.** Under ASGI a sync Django view holds its whole worker until it finishes (`bin/docker-server`), so the worker count is the maximum number of concurrent requests. The upstream default of 4 made every page (30+ parallel API calls) queue while 16 CPUs sat idle. The server override sets `GRANIAN_WORKERS=8` (about 1.2 GB each), `GRANIAN_WORKERS_MAX_RSS=2048` so Granian respawns any worker that creeps past 2 GB, and `GRANIAN_WORKERS_LIFETIME=43200` so each worker restarts every 12 hours. The count was 14 until the 2026-10-03 memory stall, when 14 workers no longer fit in RAM. `WEB_CONCURRENCY` only controls Celery (`bin/docker-worker-celery`); it is set to 2 on the `worker` service (upstream default is one process per CPU), together with `CELERY_MAX_MEMORY_PER_CHILD`. `vm.swappiness=10` (`/etc/sysctl.d/99-tuning.conf`) keeps idle-but-needed pages such as Redpanda's out of swap.
- **Measured capacity (same 5-endpoint mix, generator in another container).** 10 workers plateaued at about 27 requests per second; 14 workers reach about 33. At 48 concurrent requests the p90 fell from 4.3 s to 3.0 s; below about 12 concurrent requests nothing changed. Postgres is now the next limit (about 3 CPU cores at saturation, roughly 95 ms of Postgres CPU per request). These numbers are from the 14-worker setup. The server now runs 8 workers, so peak throughput is lower; see "Memory stall and the limits that followed".
- **Every request opens a new Postgres connection.** `CONN_MAX_AGE` is hard-coded to 0 in `posthog/settings/data_stores.py` (two places) and costs about 18 ms per request against 0.12 ms for a query on an open connection. Making it an env setting is the obvious next step, but the async AI code paths run ORM calls in pool threads that would each keep a connection. If tried, also set Postgres `idle_session_timeout` and `max_connections`, and turn on `CONN_HEALTH_CHECKS`.
- **Caddy compresses HTML and JSON only** (`encode zstd gzip` in `docker-compose.base.yml`, minimum 1 KB). Streaming `text/event-stream` responses, such as PostHog AI chat, are not compressed on purpose, so they are not buffered.
- **Hashed static files are cached for a year.** Caddy sets `Cache-Control: public, max-age=31536000, immutable` on `/static/*-<8 hash chars>.<ext>` (JS, CSS, source maps, fonts). Unhashed files such as `array.js` and `Inter.woff` keep the app's 1-hour cache.
- **Four Temporal workers run, and most queues have none.** Each worker serves exactly one task queue, and the queue names cannot be merged outside debug mode.
  - `temporal-django-worker` serves `max-ai-task-queue` (AI chat).
  - `temporal-django-worker-general` serves `general-purpose-task-queue`, which had no worker until 2026-09-20.
  - `temporal-django-worker-analytics` serves `analytics-platform-task-queue`: SQL editor exports, subscriptions and insight alerts. It was added on 2026-10-04. Before that, exports stayed in `Running` with a 2-event history until the workflow timed out after 35 minutes.
  - `temporal-django-worker-warehouse` serves `data-warehouse-task-queue`: every warehouse source sync (Google Search Console, databases, SaaS sources). Without it a connected source stays in `Running` and never imports a row. It also needs the source's own OAuth settings, such as `GOOGLE_SEARCH_CONSOLE_APP_CLIENT_ID` and `GOOGLE_SEARCH_CONSOLE_APP_CLIENT_SECRET`, and `DATA_WAREHOUSE_REDIS_HOST` and `DATA_WAREHOUSE_REDIS_PORT` (the in-stack Redis), which hold sync checkpoints and locks. `web` needs the OAuth settings as well, to start the connection.
  - All four are defined in the server-only `docker-compose.override.yml` with `extends` plus a shared environment anchor, so they reuse the AI worker's settings. Each uses about 0.9 GB of RAM.
  - The code defines 26 queues. The other 23 have no worker, including batch exports, data-warehouse imports, experiment recalculation, replay video export and the PostHog Code tasks. Add a worker only when you need that feature.
  - Find unserved queues with `tctl taskqueue describe --taskqueue <name>` (an empty poller list means nobody serves it). A workflow on such a queue shows only started and task-scheduled events.
  - A new worker needs about 90 seconds to start polling, and a task that waited in the backlog can take a few more minutes to be delivered.
- **Every Temporal worker needs the same secrets and storage settings as `web`.** The shared worker environment (`tdw_env` in the server override) carries `ENCRYPTION_SALT_KEYS`, `OBJECT_STORAGE_ENABLED` and `USE_LOCAL_SETUP`.
  - **A worker without `ENCRYPTION_SALT_KEYS` corrupts encrypted rows.** It reads a stored value as unreadable ciphertext, then saves it back encrypted again with PostHog's built-in default key. Each affected value then carries two layers and fails with `Config field '<name>' is still encrypted`, in `web` as well. Start no worker before the key is set. To repair a row, peel the layers with both the real key and the default key (`00beef0000beef0000beef0000beef00`) and save the plain value: `cryptography`'s `MultiFernet` over `settings.ENCRYPTION_SALT_KEYS` plus the default key does it, run from `manage.py shell`.
  - **`USE_LOCAL_SETUP=1` is required on every Django process, `web` included.** With `DEBUG=0` the warehouse code otherwise looks for real AWS credentials (`Unable to locate credentials`). The setting makes the write side (Temporal) and the read side (HogQL through ClickHouse) use the in-stack object store.
- **Materialized columns (dmat) do not work on this instance.** Tried 2026-09-20 and reverted. PostHog blocks `$`-prefixed system properties from materialization (`$host`, `$pathname`, `$current_url` are the most-read properties in slow queries). For custom properties the backfill fails with `Table posthog.dmat_slot_assignments does not exist`: that table and its dictionary are defined only in the newer HCL schema files (`posthog/clickhouse/hcl/`) and no numbered ClickHouse migration creates them on a hobby install. Other HCL-only objects may be missing as well. The weekly schedule function `create_or_update_weekly_dmat_backfill_schedule` is also never called anywhere in the repo. Diff the HCL schema against the live ClickHouse before retrying.
- **`web` takes about 10 minutes to start** after a recreate or reboot (migrations run at boot). Expect 502 from Caddy until it is up.

## Sync log

- **2026-09-19, merged upstream `4b1cbedf39a` (563 commits) into the fork.** Done as a merge, not a rebase. No conflicts. Reviewed the incoming diff for new gates: only the two frontend AI-subscription gates needed a patch (row 9). Checked and left alone: `_toolbar_entitlements` (already unlocked for non-Cloud), `is_team_over_ai_credit_budget` (reads a quota cache that self-hosted never fills), `LOGS_RETENTION_30D` gating (covered by the all-features-unlocked patch #1), and the new `posthog.llm.gateway_client` (only calls the URL configured in `LLM_GATEWAY_URL`, so nothing goes to a PostHog-owned host).
- **Gotcha: this local clone is shallow** (`.git/shallow`). `git fetch upstream` can report success while leaving `upstream/master` stale, and `git rev-list --count` against it is meaningless. Compare with `git ls-remote upstream refs/heads/master` before trusting the ref, and fetch `upstream master` explicitly.
- **2026-10-04, merged upstream `c5a6ef7f126` (3,211 commits, 2026-09-20 to 2026-10-04) into `master`.** Done as a merge. Seven files conflicted, all billing and license patches.
  - Upstream deleted `ee/api/license.py` and `ee/tasks/send_license_usage.py` (legacy license endpoints and usage task), so the deletions were accepted. Nothing references them.
  - Import-only conflicts in `ee/api/billing.py`, `posthog/utils.py` and `posthog/tasks/usage_report.py` were resolved as unions, then the imports that became unused were removed.
  - Upstream moved the Stripe shared-payment-token call into `BillingManager.authorize_with_shared_payment_token`. The Stripe file keeps upstream's code plus our `is_cloud()` gate, and the manager method is gated too.
  - Upstream added a whole organization billing API (`ee/api/organization_billing.py`). It reaches the billing service through `_organization_get`, `get_organization_timeseries` and `get_organization_export`, which are now gated.
  - The merge dropped the `settings` import that upstream's new webhook-signature helper in `billing_manager.py` needs. `ruff --select F821` found it. Run that check after every merge.
  - Whole-tree audit for outbound calls and limits found the four new patches above (rows 10 to 13). Verified safe or left alone: frontend Cloud-only pages (Billing, Legal documents, My tickets, startup program), the dark-launched taxonomic search, the per-team Business knowledge caps (500 and 10,000 sources), and the status-page poll (skipped without a region).
  - The TypeSafe client (`posthog.egress.typesafe`, "System One" models such as Jev) is third-party, needs `TYPESAFE_API_KEY` and makes no call without it. It serves the signals safety judge, the HogQL `jev()` and `decide()` functions, AI observability evaluation judges and replay-vision search reranking. Set the key only in the server's `.env`, never in git.
  - Organization creation is closed by default through `ORG_CREATION_ENABLED`, which is a deliberate hardening change and not a limit to remove.
  - The Python base image moved from 3.13 to 3.14 in the Dockerfile, which makes this the riskiest image build so far.
- **2026-10-07, merged upstream `2b5f974ed60` (533 commits, 2026-10-04 to 2026-10-07) into `master`.** Done as a merge with no conflicts. The working-tree patches for rows 6, 14 and 15 were committed first.
  - All patch checks in "Syncing with upstream" step 8 passed. `ruff --select F821,F811,F401` over the changed Python files passed.
  - Added row 16: the Today report's new Jev throttles are AI usage limits.
  - `docker-compose.base.yml` gained `CAPTURE_OUTPUT_*_TOPIC` variables on the three capture services. The server keeps its own copy, so port them there by hand before the next deploy, or capture may stop writing to its topics.
  - Checked and left alone: the new turn suggestions in PostHog AI (behind a PostHog feature flag that a self-hosted install never receives, and a failed draft or judge call offers nothing), the new export and ClickHouse burst throttles (abuse protection, not AI usage), and the MCP hint (hidden off Cloud and dev by upstream).
  - No new `is_cloud()` or `AvailableFeature` gate and no new outbound call to `*.posthog.com` in the incoming Python code. The `us.posthog.com` hits are test strings.
