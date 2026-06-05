"""Code-improvement sweep — the "improve my projects while I sleep" path.

Given a repo and the raw output of the local tools, it detects findings, packs them into small
atomic batches, and either opens a gated draft PR (code fixes) or files a gated issue (perf
reports). Works across Python and JS/TS/React/Next.js:

  reports keys → specialist
    ruff        → Code Quality (Python)            → draft PR
    eslint      → Code Quality (React/Next rules)  → draft PR
                  + TypeScript Type-Hygiene (any/unsafe, with tsc) → draft PR
    tsc         → TypeScript Type-Hygiene          → draft PR
    coverage    → Test & Coverage (coverage.py)    → draft PR
    js_coverage → Test & Coverage (Jest/Vitest)    → draft PR
    playwright  → Test & Coverage (E2E health)     → issue   (failing/flaky/skipped)
    bench       → Performance (pytest-benchmark)   → issue   (value: (json, baseline))
    next_build  → Performance (Next.js bundle)     → issue   (value: (data, baseline[, budget_kb]))
    lighthouse  → Performance (Core Web Vitals)    → issue   (value: json or (json, budgets))

The forge executors (open-PR / file-issue / rebase / comment) come from the runtime's configured
forge, so the same sweep targets GitHub or GitLab. Everything is injectable for testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from overnight_eng.reporting.digest import render_digest
from overnight_eng.specialists import code_quality, coverage, performance, typescript_types
from overnight_eng.specialists.base import plan_batches, run_code_work, run_issue_work
from overnight_eng.specialists.pr_coordinator import PullRequest, coordinate_prs

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.specialists.base import Finding


@dataclass
class WorkUnit:
    actor: str
    findings: list["Finding"]
    checks: list[str]
    group_by: str = "path"
    mode: str = "pr"  # "pr" (draft PR via Code Surgeon) or "issue" (file a backlog issue)


_PREFIXES = {
    "typescript_types": "types",
    "code_quality": "lint",
    "test_coverage": "test",
    "performance": "perf",
}


def _detect(reports: dict[str, Any]) -> list[WorkUnit]:
    units: list[WorkUnit] = []

    # --- Code Quality: Python (ruff) + JS/React/Next (eslint quality rules) ---
    ruff = code_quality.parse_ruff(reports.get("ruff", ""))
    if ruff:
        units.append(WorkUnit("code_quality", ruff, code_quality.DEFAULT_CHECKS, "path"))
    eslint_quality = code_quality.parse_eslint(reports.get("eslint", ""))
    if eslint_quality:
        units.append(WorkUnit("code_quality", eslint_quality, code_quality.JS_CHECKS, "path"))

    # --- TypeScript type hygiene: eslint (any/unsafe) + tsc (implicit any) ---
    ts = (typescript_types.parse_eslint(reports.get("eslint", ""))
          + typescript_types.parse_tsc(reports.get("tsc", "")))
    if ts:
        units.append(WorkUnit("typescript_types", ts, typescript_types.DEFAULT_CHECKS, "symbol"))

    # --- Test coverage: Python (coverage.py) + JS/TS (Jest/Vitest Istanbul) ---
    cov = coverage.parse_coverage(reports.get("coverage", ""))
    if cov:
        units.append(WorkUnit("test_coverage", cov, coverage.DEFAULT_CHECKS, "path"))
    js_cov = coverage.parse_js_coverage(reports.get("js_coverage", ""))
    if js_cov:
        units.append(WorkUnit("test_coverage", js_cov, coverage.JS_CHECKS, "path"))

    # --- E2E test health: Playwright (issue mode — a failing E2E may be a real bug) ---
    pw = coverage.parse_playwright(reports.get("playwright", ""))
    if pw:
        units.append(WorkUnit("test_coverage", pw, coverage.PLAYWRIGHT_CHECKS, "path", mode="issue"))

    # --- Performance (issue mode): Python benchmarks + Next bundles + Lighthouse ---
    perf: list[Finding] = []
    if reports.get("bench"):
        bench_json, baseline = reports["bench"]
        perf += performance.detect_regressions(bench_json, baseline)
    if reports.get("next_build"):
        nb = reports["next_build"]
        data, base = nb[0], (nb[1] if len(nb) > 1 else {})
        budget = nb[2] if len(nb) > 2 else None
        perf += performance.parse_next_build(data, base, budget_kb=budget)
    if reports.get("lighthouse"):
        lh = reports["lighthouse"]
        lhr, budgets = (lh if isinstance(lh, tuple) else (lh, None))
        perf += performance.parse_lighthouse(lhr, budgets)
    if perf:
        units.append(WorkUnit("performance", perf, performance.DEFAULT_CHECKS, "path", mode="issue"))

    return units


async def run_code_sweep(
    runtime: Any,
    *,
    repo_path: str,
    base_branch: str = "main",
    reports: dict[str, Any] | None = None,
    prs: list[PullRequest] | None = None,
    max_lines: int = 150,
    open_pr: Callable[[Any], str] | None = None,
    file_issue: Callable[[Any], str] | None = None,
    pr_execute: Callable[[Any], str] | None = None,
) -> str:
    """Run all code specialists + PR coordination, return the morning digest."""
    units = _detect(reports or {})

    forge = getattr(runtime, "forge", None)
    if forge is not None:
        from overnight_eng.tools.forge import (
            make_issue_executor,
            make_pr_action_executor,
            make_pr_executor,
        )

        open_pr = open_pr or make_pr_executor(forge)
        file_issue = file_issue or make_issue_executor(forge)
        pr_execute = pr_execute or make_pr_action_executor(forge)

    for unit in units:
        batches = plan_batches(unit.findings, max_lines=max_lines, group_by=unit.group_by,
                               title_prefix=_PREFIXES.get(unit.actor, "chore"))
        if unit.mode == "issue":
            run_issue_work(guard=runtime.guard, batches=batches, repo_path=repo_path,
                           actor=unit.actor, file_issue=file_issue)
        else:
            await run_code_work(
                guard=runtime.guard, surgeon=runtime.surgeon, batches=batches,
                repo_path=repo_path, base_branch=base_branch, run_checks=unit.checks,
                actor=unit.actor, max_lines=max_lines, open_pr=open_pr,
            )

    if prs:
        coordinate_prs(runtime.guard, prs, execute=pr_execute)

    return render_digest(runtime.guard.audit_log)
