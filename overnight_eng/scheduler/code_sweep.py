"""Code-improvement sweep — the "improve my projects while I sleep" path.

Given a repo and the raw output of the local tools (ESLint/tsc, ruff, coverage,
pytest-benchmark), it: detects findings, packs them into small/atomic batches, runs each
batch through the Code Surgeon, and gates the resulting draft PRs through the PolicyGuard.
Also coordinates the fleet's open PRs (rebase stale agent branches, answer review threads).

The forge executors (open-PR / rebase / comment) are injected so this whole pipeline is
testable end-to-end with a fake surgeon and no live GitHub/GitLab.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from overnight_eng.reporting.digest import render_digest
from overnight_eng.specialists import code_quality, coverage, performance, typescript_types
from overnight_eng.specialists.base import plan_batches, run_code_work
from overnight_eng.specialists.pr_coordinator import PullRequest, coordinate_prs

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.specialists.base import Finding


def _detect(reports: dict[str, Any]) -> dict[str, tuple[list["Finding"], list[str], str]]:
    """Map raw tool reports -> {actor: (findings, checks, group_by)}."""
    out: dict[str, tuple[list[Finding], list[str], str]] = {}

    ts: list[Finding] = []
    ts += typescript_types.parse_eslint(reports.get("eslint", ""))
    ts += typescript_types.parse_tsc(reports.get("tsc", ""))
    if ts:
        out["typescript_types"] = (ts, typescript_types.DEFAULT_CHECKS, "symbol")

    ruff = code_quality.parse_ruff(reports.get("ruff", ""))
    if ruff:
        out["code_quality"] = (ruff, code_quality.DEFAULT_CHECKS, "path")

    cov = coverage.parse_coverage(reports.get("coverage", ""))
    if cov:
        out["test_coverage"] = (cov, coverage.DEFAULT_CHECKS, "path")

    bench = reports.get("bench")
    if bench:
        bench_json, baseline = bench
        perf = performance.detect_regressions(bench_json, baseline)
        if perf:
            out["performance"] = (perf, performance.DEFAULT_CHECKS, "path")

    return out


async def run_code_sweep(
    runtime: Any,
    *,
    repo_path: str,
    base_branch: str = "main",
    reports: dict[str, Any] | None = None,
    prs: list[PullRequest] | None = None,
    max_lines: int = 150,
    open_pr: Callable[[Any], str] | None = None,
    pr_execute: Callable[[Any], str] | None = None,
) -> str:
    """Run all code specialists + PR coordination, return the morning digest."""
    reports = reports or {}
    detected = _detect(reports)

    for actor, (findings, checks, group_by) in detected.items():
        batches = plan_batches(findings, max_lines=max_lines, group_by=group_by,
                               title_prefix=_prefix_for(actor))
        await run_code_work(
            guard=runtime.guard,
            surgeon=runtime.surgeon,
            batches=batches,
            repo_path=repo_path,
            base_branch=base_branch,
            run_checks=checks,
            actor=actor,
            max_lines=max_lines,
            open_pr=open_pr,
        )

    if prs:
        coordinate_prs(runtime.guard, prs, execute=pr_execute)

    return render_digest(runtime.guard.audit_log)


_PREFIXES = {
    "typescript_types": "types",
    "code_quality": "lint",
    "test_coverage": "test",
    "performance": "perf",
}


def _prefix_for(actor: str) -> str:
    return _PREFIXES.get(actor, "chore")
