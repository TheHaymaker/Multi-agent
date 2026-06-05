# Running Overnight Engineering on your local machine

This is the **road-to-runnable** for getting the fleet working on your own machine, against your
own repos, filing real issues and opening real draft PRs while you sleep.

It's honest about what's done vs. what still needs wiring. The system is built **logic-first**:
every detector, planner, guardrail, and the digest are pure, dependency-light, and covered by
107 passing tests. What remains is the **integration glue** — making the gated actions actually
land in GitHub/GitLab, feeding the code agents your real repos, and turning on the LLM + Code
Surgeon. Those are the phases below.

> TL;DR ordering: **Phase 0** (works today) → **Phase 1** infra/secrets → **Phase 2** real forge
> writes → **Phase 3** point it at your repos → **Phase 4** Code Surgeon edits → **Phase 5** LLM +
> memory + traces → **Phase 6** run it overnight. You get value as early as Phase 2 (real triaged
> issues) and Phase 3 (real draft PRs for lint/types), before any LLM is involved.

---

## Status snapshot — real vs. stubbed

| Capability | State | Where |
|---|---|---|
| Detectors/parsers (Sentry, ruff, ESLint, tsc, coverage, Jest/Vitest, Playwright, Next bundle, Lighthouse, pytest-benchmark) | ✅ done + tested | `overnight_eng/specialists/*`, `triage/*`, `sources/*` |
| Small-PR batching, dedupe, severity, Jira reconcile | ✅ done + tested | `specialists/base.py`, `triage/*`, `specialists/jira_backlog.py` |
| PolicyGuard (propose-only gate), audit log, morning digest | ✅ done + tested | `tools/policy_guard.py`, `reporting/digest.py` |
| GitHub/GitLab forge **mapping** (PR vs MR, draft, rebase) | ✅ done + tested | `tools/forge.py` |
| Forge **execution** (actually calling the API) | ⚠️ **stub** — returns a descriptive string | `runtime.py::_invoke_mcp_tool` |
| Signal-triage daemon path (queue → dedupe → file issue) | ✅ wired (deterministic, no LLM) | `scheduler/sweep.py`, `scheduler/jobs.py` |
| **Code-improvement sweep** over local repos | ⚠️ **not wired** — `run_code_sweep` exists + tested but nothing gathers reports or calls it | `scheduler/code_sweep.py` |
| Code Surgeon real edits (Claude Agent SDK) | ⚠️ **no-ops if SDK/auth absent**; worktree+push scaffolding present | `workers/code_surgeon.py` |
| ADK LLM agents (orchestrator + specialists) | ⚠️ built but **not driven** by the sweep yet | `agents/orchestrator.py` (`build_app`) |
| Long-term memory (Mem0/pgvector) | ✅ in-memory fallback; real store needs config | `memory/store.py` |
| Local infra (Postgres/Redis/Qdrant/Langfuse) | ✅ compose file ready | `docker-compose.yml` |
| Observability (Langfuse via OTel) | ⚠️ exporter wired; needs keys + agents actually running | `runtime.py::init_observability` |

---

## Prerequisites

- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Docker + Docker Compose** (for Postgres/Redis/Qdrant/Langfuse)
- **Node 20+** — the JS/TS toolchains (ESLint, tsc, vitest/jest, Playwright, `next build`,
  Lighthouse) and several MCP servers run via `npx`
- **git** with a push-capable credential for a **bot identity** (least-scope token)
- Optional: **`sops` + age** for encrypted secrets (`.env.sops`); a **Claude Pro/Max** login for
  the subscription-backed Code Surgeon; **ngrok/cloudflared** to receive webhooks

---

## Phase 0 — Prove the core (works today, no services)

```bash
git clone <this-repo> && cd Multi-agent
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
make test            # 107 tests, ~0.2s, zero external services
make triage P=tests/fixtures/sentry_issue_alert.json   # dry-run; prints a digest
```

If this passes you have a working detection/planning/gating core. Everything below makes its
gated actions real.

> **Dependency reality check.** `pyproject.toml` pins `google-adk`, `claude-agent-sdk`,
> `mem0ai`, `langfuse`, etc. with optimistic lower bounds. Before Phase 4/5, run
> `uv pip install -e .` (no `[dev]`) once and **pin a working set** with `uv lock` — some of
> these move fast and an exact lockfile saves pain. Phases 0–3 don't need the heavy agent deps.

---

## Phase 1 — Local infra + secrets + health

```bash
cp .env.example .env       # fill in the secrets below
docker compose up -d       # postgres, redis, qdrant, langfuse
overnight health           # prints config + flags any MISSING required secrets
```

Minimum secrets to start (in `.env`):

| Key | Why | Phase it unlocks |
|---|---|---|
| `GITHUB_TOKEN` (or `GITLAB_TOKEN`) | bot token, least scope (issues + PRs) | 2, 3 |
| `SENTRY_AUTH_TOKEN` | triage source | 2 |
| `ANTHROPIC_API_KEY` | ADK LLM glue (cheap) | 5 |
| `FORGE=github`\|`gitlab` | which forge the deterministic writes target | 2 |
| `OVERNIGHT_DRY_RUN=true` | keep it side-effect-free until you're ready | all |

Keep `OVERNIGHT_DRY_RUN=true` through Phases 2–3 while you watch the digest; flip to `false`
only when you trust what it proposes.

---

## Phase 2 — Make the forge writes real (first real value: triaged issues)

**Goal:** a Sentry signal becomes a real GitHub issue (deduped, severity-labelled).

The only thing standing in the way is `runtime.py::_invoke_mcp_tool`, which currently returns a
descriptive string instead of calling the API. Two options:

1. **Recommended — direct REST for deterministic writes.** The sweep's create-issue / open-PR /
   rebase / comment calls are deterministic; drive them with `PyGithub` / `python-gitlab` /
   `jira` rather than MCP. Replace the body of `_build_forge`'s `invoke` with a thin client that
   maps the tool names already defined in `tools/forge.py` (`create_issue`,
   `create_pull_request`, `update_pull_request_branch`, …) to REST calls. MCP stays for the
   *LLM* agents' exploratory use (Phase 5).
2. **Alternative — MCP client.** Open an `mcp` `ClientSession` per server and `call_tool(name,
   args)`. More faithful to the Dev Signal pattern, but heavier for deterministic writes.

**Verify:**
```bash
# dry-run first — see the intended action in the digest
overnight triage tests/fixtures/sentry_issue_alert.json
# then for real against a throwaway repo:
OVERNIGHT_DRY_RUN=false overnight triage tests/fixtures/sentry_issue_alert.json --execute
# → one issue created; run it again → NO duplicate (dedupe via memory)
```

Acceptance: exactly one issue per unique fingerprint; the digest lists it with a URL.

---

## Phase 3 — Point it at your repos (first real value: lint/type/coverage PRs)

**Goal:** the code agents run their tools over *your* projects and open small draft PRs.

This is the biggest missing piece. `run_code_sweep(...)` is fully built and tested but **nothing
gathers the tool reports or calls it**. Add three things:

1. **A projects config** — e.g. `projects.yaml`:
   ```yaml
   projects:
     - name: my-next-app
       repo_path: /home/me/code/my-next-app
       forge_repo: me/my-next-app        # owner/name (GitHub) or group/proj (GitLab)
       base_branch: main
       reports:                          # commands whose stdout/files feed the detectors
         eslint:     "npx eslint . -f json"
         tsc:        "npx tsc --noEmit"
         js_coverage: "npx vitest run --coverage --reporter=json && cat coverage/coverage-summary.json"
         playwright: "npx playwright test --reporter=json || true"
         next_build: "next build"        # paired with a stored baseline
         lighthouse: "npx --yes @lhci/cli autorun"
     - name: my-py-lib
       repo_path: /home/me/code/my-py-lib
       forge_repo: me/my-py-lib
       reports:
         ruff:     "ruff check . --output-format=json"
         coverage: "coverage run -m pytest && coverage json -o- "
   ```
2. **A report runner** — `scheduler/projects.py`: for each project, run each `reports.*` command
   in `repo_path`, capture stdout (or the named file), and build the `reports` dict
   `run_code_sweep` already expects. Store per-route Next.js/bench baselines under
   `.overnight/baselines/<project>.json` and update them on green runs.
3. **A daemon job** — add `code_nightly(runtime)` in `scheduler/jobs.py` that loops projects,
   calls `await run_code_sweep(runtime, repo_path=..., base_branch=..., reports=..., prs=...)`,
   and merges its digest with the signal-triage digest. Register it on the nightly cron next to
   the existing `nightly` job. (Optionally fetch open PRs per project to feed the PR-coordinator
   `prs=` arg so stale agent branches get rebased.)

**Verify:** point it at one repo with a known `any` or a missing React-hook dep, run
`overnight sweep` with `OVERNIGHT_DRY_RUN=false` against a throwaway branch, and confirm a small
draft PR appears (≤150 changed lines). Until Phase 4, the Code Surgeon makes no edits, so PRs
will be empty/skipped — use this phase to validate *detection + batching + gating* against real
reports, then turn on edits in Phase 4.

---

## Phase 4 — Turn on the Code Surgeon (real edits in draft PRs)

**Goal:** the draft PRs actually contain the fix.

`workers/code_surgeon.py` already sets up an isolated **git worktree** on an agent-owned branch,
enforces the line cap, runs your checks, and pushes — but `_apply_edits` **no-ops unless the
Claude Agent SDK is installed and authenticated**.

1. `uv pip install claude-agent-sdk` (pin the version once it installs cleanly).
2. **Auth** — for the flat-rate subscription path, log in once with the Claude CLI
   (`claude` / `/login`, Pro/Max); the Agent SDK inherits that session. For pay-per-token, set
   `ANTHROPIC_API_KEY` and `SURGEON_USE_SUBSCRIPTION=false`. (See the README "Claude auth split".)
3. **Repo requirements** — each `repo_path` must be a real git clone with a push remote and the
   bot credential available to `git push` (the worktree pushes the agent branch).
4. **Sandboxing** — the surgeon runs `Bash(npm test:*)` / `tsc` / `git diff` in the worktree. Run
   the daemon as a low-privilege user (or in a container) since it executes your repos' test
   commands. Keep `allowed_tools` tight in `_apply_edits`.

**Verify:** rerun Phase 3 against the throwaway branch — the draft PR now contains a minimal,
checked diff. Confirm the line-cap rejection works by pointing it at a file that needs a big
change (it should skip with a "diff too large" note in the digest).

---

## Phase 5 — LLM reasoning, memory, and traces (optional but high-value)

Phases 2–4 are deterministic. This phase adds the judgement layer.

- **Drive the ADK orchestrator.** `agents/orchestrator.py::build_app` is built but the sweep uses
  the deterministic path. To get RCA hypotheses on triage and nuanced routing, invoke the ADK
  `Runner` over `runtime.app` inside `scheduler/sweep.py::triage_one` (lazily, as the docstring
  anticipates), passing the signal as the user turn and letting the triage sub-agent enrich the
  issue body. Keep the deterministic dedupe/severity as the gate around it.
- **Long-term memory.** Set `QDRANT_URL` (compose already runs Qdrant) and configure `mem0ai` in
  `memory/store.py` so preferences and "don't refile X" persist across nights instead of the
  in-memory fallback.
- **Observability.** Create a Langfuse project (UI at `http://localhost:3000`), put its keys in
  `.env`, and confirm `init_observability` exports OTel spans — you'll see a hierarchical trace
  per orchestrator run.

---

## Phase 6 — Run it overnight

```bash
OVERNIGHT_DRY_RUN=false overnight serve     # FastAPI ingress on :8080 + APScheduler
```

- **Webhooks (optional, for event-driven triage):** expose `:8080` with `ngrok http 8080` (or
  cloudflared) and point your Sentry alert webhook at `/webhooks/sentry`. Without this, the
  scheduled poll + nightly cron still run.
- **Schedule:** `POLL_MINUTES` (default 15) drains the queue; `DIGEST_HOUR` (default 6) fires the
  full nightly sweep + digest. Set `SLACK_WEBHOOK_URL` to get the morning digest in Slack;
  otherwise it prints.
- **Or via Docker:** `docker compose up -d` runs the `app` service as `overnight serve` with the
  infra wired. Mount your project repos into the container and reference those paths in
  `projects.yaml`.

**Minimum viable nightly run** = Phase 1 infra + Phase 2 forge writes + Phase 3 projects/report
runner + (Phase 4 for edits). LLM/memory/traces (Phase 5) are additive.

---

## Known gaps & risks (with pointers)

| Gap | Impact | Fix in |
|---|---|---|
| `_invoke_mcp_tool` returns a string | no real forge writes | `runtime.py` (Phase 2) |
| No projects config / report runner / `code_nightly` job | code agents never run on real repos | new `projects.yaml` + `scheduler/projects.py` + `scheduler/jobs.py` (Phase 3) |
| ADK `Runner` not invoked by the sweep | no LLM RCA/routing | `scheduler/sweep.py` (Phase 5) |
| Code Surgeon no-ops without SDK/auth | empty PRs | `workers/code_surgeon.py` (Phase 4) |
| Dep versions are optimistic bounds | install friction | `uv lock` (Phase 0/4) |
| Langfuse v2 image + placeholder `NEXTAUTH_SECRET`/`SALT` | traces UI won't auth | `docker-compose.yml` (Phase 5) |
| Surgeon executes repo test commands | runs untrusted code | sandbox/low-priv user (Phase 4) |
| `next_build`/benchmark baselines | first run has nothing to compare | baseline store in `scheduler/projects.py` (Phase 3) |

## Guardrails that are already on (so you can sleep)

- **Propose-only**: `PolicyGuard` denies push-to-main, merge, force-push, closing others' work,
  and branch deletes; it only allows issues, comments, agent-owned branches, rebases, and
  **draft** PRs/MRs. Flip `OVERNIGHT_DRY_RUN=true` for a zero-side-effect rehearsal.
- **Small PRs**: every code change is capped (default 150 lines) and rejected/re-scoped if larger.
- **Dedupe**: fingerprinted signals won't refile; recurrences get a bump comment.
- **Audit log**: every gated decision (allowed/denied, executed, reason) is recorded and shows up
  in the morning digest — including what it deliberately skipped.
