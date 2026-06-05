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
make test                     # 51 tests — core logic runs with zero external services
make up                       # postgres + redis + qdrant + langfuse + app via docker compose
make health                   # verify config + which MCP toolsets connect

# Try the MVP vertical on a recorded Sentry payload (dry-run = zero side effects):
make triage P=tests/fixtures/sentry_issue_alert.json
```

`make serve` runs the overnight daemon: a FastAPI webhook ingress (Sentry/GitHub → Redis queue) plus an
APScheduler that polls every 15 min and fires a full sweep + digest at 06:00.

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
| Root Orchestrator | schedules, routes, owns memory, writes the digest | ✅ MVP |
| **Signal Triage** | Sentry/Grafana → dedupe, severity, RCA → file backlog issue | ✅ MVP vertical |
| Backlog/Jira | polls Jira, reconciles, proposes priority | planned |
| PR Coordinator | comments, rebases its own stale branches, opens draft PRs | planned |
| Code Quality | ruff/pylint/semgrep → findings → draft PRs | planned |
| Test & Coverage | coverage + mutation testing → draft tests | planned |
| Performance | benchmarks/profilers → regressions | planned |
| **TypeScript Type-Hygiene** | clears `any`/unsafe casts in **small, atomic PRs** (line cap) | planned |
| Code Surgeon (worker) | Claude Agent SDK; edits in a git worktree, opens draft PR | ✅ skeleton |

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
  memory hydration, and the **Sentry→issue e2e** (one issue filed; duplicate delivery files nothing new).
- `make triage P=...` — one-shot triage of a payload, dry-run by default.
- After `make up`: open Langfuse at `http://localhost:3000` to see hierarchical agent traces.

## Layout
```
overnight_eng/
├── agents/        orchestrator + signal_triage (ADK) + shared callbacks
├── tools/         mcp_config (Sentry/Grafana/GitHub/GitLab/Jira) + policy_guard
├── triage/        dedupe + severity (pure, heavily tested)
├── sources/       webhook payload → Signal normalizers
├── memory/        FleetMemory (Mem0/pgvector + in-memory fallback)
├── workers/       code_surgeon (Claude Agent SDK)
├── scheduler/     sweep, jobs (APScheduler), webhook (FastAPI)
├── reporting/     morning digest
├── runtime.py     Plan A service bindings (swap here for Plan B)
└── cli.py         overnight {health,triage,sweep,serve}
deploy/terraform/  Plan B (Cloud Run + Vertex + Scheduler→Pub/Sub)
```
