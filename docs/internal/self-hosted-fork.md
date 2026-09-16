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
| 3 | No outbound calls to PostHog's own services | Nothing in this deployment should call `*.posthog.com`, `*.i.posthog.com`, `posthogstatus.com`, or the license/billing/usage-report endpoints. Analytics capture (`posthoganalytics.capture()`), billing sync, license usage, the adblock probe, and the Max search sitemap fetch are the known callers found so far. | Not yet applied |
| 4 | Admin/hidden-endpoint IP allowlist | Django admin and any endpoint meant to stay private must reject requests from IPs outside a configured allowlist. | Not yet applied |

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

Update this table (add rows, change "Status", link the commit) every time a
must-have patch is added, changed, or removed. Never delete a row silently —
if a patch is retired, say why.

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
