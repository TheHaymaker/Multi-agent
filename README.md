# Overnight Engineering 🌅

A **local-first multi-agent system** that runs on your machine overnight: it triages Sentry/Grafana
signals, polls Jira, coordinates PRs, and proposes code-quality / test / performance / TypeScript-type
improvements — then hands you a **morning digest** of what it did and what it deliberately skipped.

It's modeled on Google's 4-part **"Dev Signal"** series (ADK + MCP + memory, tested locally, deployed to
Cloud Run), but inverted: **open-source / local stack first** (Plan A), with the **GCP stack kept
alongside for comparison** (Plan B). Because the orchestration spine — **Google ADK**, which is itself
open-source and model-agnostic — is identical across both, only the *service bindings* differ, so a
future lift to Google Cloud is a binding swap, not a rewrite.

> Full design rationale and the side-by-side Plan A vs Plan B comparison live in the plan file referenced
> in the project history.

---

## Quickstart (Plan A — local)

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY, GITHUB_TOKEN, SENTRY_AUTH_TOKEN
make dev                      # install runtime + dev deps
make test                     # 107 tests — core logic runs with zero external services
make up                       # postgres + redis + qdrant + langfuse + app via docker compose
make health                   # verify config + which MCP toolsets connect

# Try the MVP vertical on a recorded Sentry payload (dry-run = zero side effects):
make triage P=tests/fixtures/sentry_issue_alert.json
```

`make serve` runs the overnight daemon: a FastAPI webhook ingress (Sentry/GitHub → Redis queue) plus an
APScheduler that polls every 15 min and fires a full sweep + digest at 06:00.

> **Getting it actually running on your machine** (real issues/PRs against your repos): follow the phased
> road-to-runnable in **[`docs/RUNNING_LOCALLY.md`](docs/RUNNING_LOCALLY.md)** — it's explicit about what's
> done vs. what still needs wiring, and you get value as early as Phase 2.

---

## How it works

```
webhooks ─▶ Redis queue ─▶ Root Orchestrator (ADK) ─▶ specialists ─▶ PolicyGuard ─▶ forge/git
                                  │                                        │
                            FleetMemory (Mem0/pgvector)            audit log ─▶ morning digest
```

- **Capabilities via MCP.** Every integration (Sentry, Grafana, GitHub, GitLab, Jira) is an MCP server
  wrapped by ADK's `McpToolset` — `overnight_eng/tools/mcp_config.py`.
- **Hierarchical agents + memory.** A root orchestrator routes to specialist sub-agents; short-term
  handoff via session state, cross-night recall via `FleetMemory` (`overnight_eng/memory/store.py`).
- **De-dup so nothing is refiled.** `overnight_eng/triage/dedupe.py` fingerprints signals (reusing
  Sentry's own grouping when present) — recurrences get a bump comment, not a duplicate issue.

### The agent fleet
| Agent | Does | Status |
|---|---|---|
| Root Orchestrator | schedules, routes, owns memory, writes the digest | ✅ |
| **Signal Triage** | Sentry/Grafana → dedupe, severity, RCA → file backlog issue | ✅ |
| PR Coordinator | rebases its own stale branches, answers review threads, plans draft-PR edits | ✅ |
| Code Quality | **Python** ruff + **JS/TS/React/Next** ESLint (hooks, `jsx-key`, `@next/next`, a11y, import cycles) → small draft PRs | ✅ |
| Test & Coverage | **Python** coverage.py + **JS/TS** Jest/Vitest (Istanbul) gaps → draft tests; **Playwright** failing/flaky/skipped → issues | ✅ |
| Performance | **Python** pytest-benchmark + **Next.js** bundle size (First Load JS) + **Lighthouse** Core Web Vitals → issues | ✅ |
| **TypeScript Type-Hygiene** | clears `any`/unsafe casts in **small, atomic PRs** (line cap) | ✅ |
| Code Surgeon (worker) | Claude Agent SDK; edits in a git worktree, opens draft PR | ✅ |
| Backlog/Jira | polls Jira, reconciles cross-system, dedupes, proposes priority | ✅ |

### GitHub & GitLab parity
One forge abstraction (`overnight_eng/tools/forge.py`) maps neutral actions (issue, **draft
PR/MR**, comment, rebase) to each provider — GitHub draft PRs vs GitLab `Draft:` merge
requests, `update_pull_request_branch` vs native `rebase_merge_request`. Set `FORGE=github`
or `FORGE=gitlab`; the sweep executors pick the right calls automatically.

### Propose-only autonomy
Every side-effecting action goes through one gate — `overnight_eng/tools/policy_guard.py`:
- **Allowed:** file issues, comment, work on *agent-owned* branches, rebase onto base, open/update **draft** PRs.
- **Denied:** push to `main`/protected, merge, force-push, close others' work, delete branches.
- Plus dry-run mode, an append-only audit log, and least-scope bot credentials. It's the most heavily
  tested module in the repo.

### Claude auth split (saves money)
- **Code Surgeon** (the expensive multi-file editing) runs on the **Claude Agent SDK**, which can
  authenticate headless on a **Pro/Max subscription** (flat-rate) — `SURGEON_USE_SUBSCRIPTION=true`.
- **ADK orchestration glue** (cheap classify/route/dedupe) uses a small **API key** via LiteLLM — LiteLLM
  can't use the subscription, only an API key.
- Net: subscription carries the heavy code work; a small key carries the glue. See `.env.example`.

---

## Plan B — Google Cloud (the Dev Signal mirror)
Same agent code; swap only `overnight_eng/runtime.py` bindings (Vertex Memory Bank + Vertex Session
Service + Cloud Trace + Secret Manager) and deploy with `deploy/terraform/` (Cloud Run + Artifact
Registry + Cloud Scheduler→Pub/Sub for the overnight loop). The one structural difference: Cloud Run is
request-driven, so "overnight" becomes a scheduled trigger rather than a long-running daemon.

---

## Verification
- `make test` — PolicyGuard (deny merge/main, allow draft PR/issue), dedupe, Sentry normalize, severity,
  memory hydration, the **Sentry→issue e2e**, the small-PR **batching planner**, every specialist parser
  (tsc/ESLint, ruff, coverage, benchmark), the **code-sweep e2e** (findings → small draft PRs +
  stale-branch rebase, all gated), **forge parity** (GitHub PRs vs GitLab `Draft:` MRs/rebase), and
  **Jira reconciliation** (cross-system de-dup + priority proposals).
- `make triage P=...` — one-shot triage of a payload, dry-run by default.
- After `make up`: open Langfuse at `http://localhost:3000` to see hierarchical agent traces.

## Layout
```
overnight_eng/
├── agents/        orchestrator + signal_triage (ADK) + shared callbacks
├── specialists/   base (Findings + small-PR batching + code-work driver), pr_coordinator,
│                  code_quality, coverage, performance, typescript_types, jira_backlog
├── tools/         mcp_config (Sentry/Grafana/GitHub/GitLab/Jira) + policy_guard + forge
├── triage/        dedupe + severity (pure, heavily tested)
├── sources/       webhook payload → Signal normalizers
├── memory/        FleetMemory (Mem0/pgvector + in-memory fallback)
├── workers/       code_surgeon (Claude Agent SDK)
├── scheduler/     sweep, code_sweep, jobs (APScheduler), webhook (FastAPI)
├── reporting/     morning digest
├── runtime.py     Plan A service bindings (swap here for Plan B)
└── cli.py         overnight {health,triage,sweep,serve}
deploy/terraform/  Plan B (Cloud Run + Vertex + Scheduler→Pub/Sub)
```
