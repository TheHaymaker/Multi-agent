# Agent roadmap — improvements & new agents

Ideas to make the fleet more valuable for a **local Python + TypeScript / Next.js / React**
workflow. Everything here is scoped to the existing architecture, which makes additions cheap:

> **The extension pattern.** A new capability = a pure `parse_* -> list[Finding]` function, a
> choice of **PR mode** (`run_code_work` → small gated draft PR) or **issue mode**
> (`run_issue_work` → gated backlog issue), and one `WorkUnit` registered in
> `scheduler/code_sweep.py`. It then inherits small-PR batching, the PolicyGuard, GitHub/GitLab
> parity, the Code Surgeon, and the morning digest for free. New *event* sources are a
> `normalize_* -> Signal` in `sources/`. Keep parsers pure + unit-tested; that's the moat.

Each item is tagged **Effort** (S/M/L) · **Risk** (low/med/high) · **Mode** (PR / issue / event).
"⭐" marks the highest leverage for an overnight, local, propose-only setup.

---

## Part 1 — Improvements to existing agents

### Signal Triage (`specialists/signal_triage.py`, `sources/`)
- ⭐ **Likely-culprit attribution.** Map the Sentry stack frames to files, `git blame` the
  offending lines, and correlate with recent commits/PRs → name the suspected change in the
  issue. *(M · low · issue)*
- ⭐ **Impact-based severity.** Use event volume, % sessions, and users-affected from Sentry
  (and Grafana error-rate) instead of only keyword heuristics in `triage/severity.py`. *(S · low)*
- **Incident grouping.** Cluster related errors across services into one incident issue rather
  than N issues (extend `triage/dedupe.py` with cross-signal correlation). *(M · med)*
- **Learned snoozing.** Remember human "won't fix"/close decisions in `FleetMemory` and stop
  re-surfacing known-noisy signals. *(S · low)*
- **Repro stub.** When the trace maps to code, attach a failing test skeleton to the issue so the
  fix starts with a red test. *(M · med · PR)*

### PR Coordinator (`specialists/pr_coordinator.py`)
- ⭐ **CI-failure triage.** Detect the fleet's PRs that are red, pull the CI logs, diagnose, and
  hand a fix-up to the Code Surgeon (kick-it-til-green, but propose-only on the agent's own
  branch). *(L · med · PR)*
- **Trivial-conflict auto-resolution.** Resolve lockfile/import-order/generated-file conflicts on
  rebase; escalate semantic conflicts with a comment. *(M · med)*
- **Review aids.** Post a review checklist / risk summary on large PRs; flag a diff that changes
  behavior but adds no tests. *(S · low · issue/comment)*
- **Draft→ready signal.** When checks pass, comment "ready for review" (don't flip state — stays
  propose-only). *(S · low)*

### Code Quality (`specialists/code_quality.py`)
- ⭐ **Wire up semgrep.** The instruction mentions it but only ruff/ESLint are parsed. Add a
  `parse_semgrep` for bug-class + security rules (incl. custom org rules). *(S · low · PR/issue)*
- ⭐ **Dead-code & unused deps.** `knip`/`ts-prune` (TS) and `vulture`/`deptry` (Py) → remove dead
  exports and drop unused dependencies in tiny PRs. Great overnight wins, low blast radius. *(M · low · PR)*
- **Complexity guardrails.** Flag functions over a cyclomatic/length threshold (ESLint
  `complexity`, `radon` for Py) → propose extraction. *(S · low · issue)*

### Test & Coverage (`specialists/coverage.py`)
- ⭐ **Mutation testing (promote from "plug-in").** Stryker (TS) / mutmut (Py): surviving mutants
  become "your tests don't catch this" findings → add killing assertions. Far higher signal than
  line coverage. *(L · med · PR)*
- **Flaky history, not snapshots.** Track Playwright/Jest flakiness across N nights in memory and
  quarantine the persistently flaky, instead of reacting to a single report. *(M · med · issue)*
- **Characterization tests.** Before a risky refactor, generate tests that pin current behavior of
  untested legacy code. *(M · med · PR)*
- **Obsolete-snapshot cleanup.** Delete stale Jest/Vitest snapshots. *(S · low · PR)*

### Performance (`specialists/performance.py`)
- ⭐ **React render perf.** `eslint-plugin-react-perf` / React Compiler diagnostics + detect heavy
  `"use client"` trees, missing memoization, and unnecessary re-renders. *(M · med · issue/PR)*
- ⭐ **Next.js server/client boundary.** Flag large client bundles caused by client components
  that should be server components, unoptimized `<img>`, blocking awaits that should stream
  (Suspense). *(M · med · issue)*
- **Query/N+1 perf.** Mine Grafana / `pg_stat_statements` for slow queries and ORM logs for N+1s
  → issues with the offending call site. *(L · med · issue)*

### Backlog/Jira (`specialists/jira_backlog.py`)
- **Bidirectional status sync.** Move a ticket to Done when its linked PR merges; link PRs to
  tickets via branch/commit conventions. *(M · med · event)*
- **Sprint hygiene.** Surface stale/aging in-progress tickets, missing estimates, unassigned
  high-priority. *(S · low · issue)*
- **Standup / weekly digest.** Summarize the night's + week's activity into a posted summary. *(S · low)*

### Cross-cutting infra (`agents/orchestrator.py`, `specialists/base.py`, `workers/code_surgeon.py`)
- ⭐ **Nightly budget + prioritization.** Rank findings by impact and respect a cap (e.g. "≤ N PRs
  / token budget per night") so you wake up to a curated set, not 50 PRs. *(M · low)*
- ⭐ **Cross-agent dedupe / combined PRs.** A file flagged by quality + types + dead-code should
  become *one* PR, not three. Dedupe `Finding`s by path before batching. *(M · low)*
- **Preference learning.** Track which draft PRs get merged vs closed and auto-tune thresholds /
  which rule families to act on. *(M · med)*
- **Test-impact analysis.** Run only the tests affected by the diff instead of the whole suite —
  faster Code Surgeon verification. *(M · med)*
- **Self-review pass.** Run a `/code-review`-style critique before opening each PR; drop weak
  changes. *(S · low)*

---

## Part 2 — Entirely new agents

| Agent | What it does | Value | Mode | Effort · Risk |
|---|---|---|---|---|
| ⭐ **Dependency / Supply-chain** | `npm audit` / `pnpm`, `pip-audit`, `osv-scanner`, outdated checks → grouped, minimal **bump PRs** with changelog + risk notes; respects lockfiles | A local Dependabot/Renovate that wakes you to safe, reviewed updates instead of a PR firehose | PR | M · low |
| ⭐ **Security / SAST** | `semgrep` security packs + secret scanning (`gitleaks`/`trufflehog`) + dangerous patterns (`eval`, `dangerouslySetInnerHTML`, SSRF, injection) | Catches real vulns nightly; secret-leak detection alone justifies it | issue (+ PR for trivial) | M · med |
| ⭐ **Refactor / Tech-debt hotspots** | Find high-churn × high-complexity files, duplication (`jscpd`), god-functions → incremental small-PR refactors; maintain a debt ledger | Pays down debt continuously in low-risk slices — exactly what small-PR discipline is for | PR | L · med |
| ⭐ **Migration driver** | Drive long migrations one safe slice per PR: Next `pages`→`app`, React class→function, deprecated-API codemods, ESM, lib major bumps | The small-PR + propose-only model is *ideal* for risky migrations; turns a scary project into nightly nibbles | PR | L · med |
| **Accessibility (a11y)** | `axe-core` via Playwright on key routes + deeper `jsx-a11y` → contrast, ARIA, keyboard-nav, alt-text fixes | Frontend correctness most teams skip; many fixes are tiny PRs | PR + issue | M · low |
| **Documentation drift** | Detect code↔docs drift: stale README/JSDoc/docstrings, undocumented new env vars/exports, outdated examples; refresh `CHANGELOG` | Docs stay true without manual upkeep | PR | M · low |
| **API / schema contract** | Detect breaking changes in OpenAPI/GraphQL/tRPC/Prisma vs `main`; flag consumers; keep generated types in sync; catch FE-type ↔ BE-schema drift | Prevents silent contract breaks across a TS frontend + backend | issue (+ PR for codegen) | M · med |
| **Release / Changelog** | Assemble release notes from merged PRs, suggest a semver bump, draft a release PR, verify version/lockfile/changelog consistency | Removes release-prep toil | PR | S · low |
| **Observability / SLO watcher** | Proactively read Grafana trends (error-budget burn, latency creep, memory growth) *before* they page → "investigate" issues with the panel | Shifts triage from reactive to proactive | event/issue | M · med |
| **i18n** | Find hardcoded JSX strings + missing locale keys in Next i18n → extract & stub translations | Keeps localization honest as the UI grows | PR + issue | S · low |
| **DX / build-health** | Track build/dev-server/lint/test durations and CI flakiness across nights; keep `.nvmrc`/`engines`/tooling consistent across repos | Stops slow, drifting toolchains | issue | S · low |

---

## If you build only three next (for this stack)

1. **Dependency / Supply-chain agent** — highest value-to-risk: nightly, safe, reviewed bump PRs
   + vuln alerts. Pairs perfectly with "wake up to it handled."
2. **Security / SAST + secret scanning** — semgrep + gitleaks catch issues you can't afford to
   miss; mostly issue-mode so it's low-risk to turn on.
3. **Refactor/Tech-debt _or_ Migration driver** — both are the killer app for the small-PR engine.
   Pick Migration if you have a specific one looming (e.g. app-router), Tech-debt for steady
   continuous cleanup.

Plus two cheap, force-multiplying infra upgrades that make *all* agents nicer to live with:
**nightly budget/prioritization** and **cross-agent dedupe (combined PRs)** — without them, a
multi-agent fleet can bury you in PRs.

---

## Why these are cheap to add (recap)

Each new agent reuses what's already built and tested:
- `specialists/base.py` — `Finding`, `plan_batches` (small-PR caps), `run_code_work` (PR mode),
  `run_issue_work` (issue mode)
- `tools/policy_guard.py` — propose-only gating + audit log
- `tools/forge.py` — GitHub/GitLab parity
- `workers/code_surgeon.py` — isolated-worktree edits with line caps
- `scheduler/code_sweep.py` — register a `WorkUnit(actor, findings, checks, group_by, mode)`
- `reporting/digest.py` — it shows up in the morning digest automatically

So most agents above are **a few hundred lines of pure parser + a WorkUnit + tests**, not new
infrastructure. Start by adding the parser with unit tests (the established pattern), then wire
the WorkUnit once the report-runner from `docs/RUNNING_LOCALLY.md` Phase 3 exists.
