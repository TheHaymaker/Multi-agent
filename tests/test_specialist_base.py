"""Tests for the shared specialist base: small-PR batching + the code-work driver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from overnight_eng.models import ActionType, Severity
from overnight_eng.specialists.base import (
    ChangeBatch,
    Finding,
    plan_batches,
    run_code_work,
)
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.workers.code_surgeon import ChangeProposal


def _f(path: str, lines: int = 10, sev: Severity = Severity.MEDIUM, symbol: str = "") -> Finding:
    return Finding(kind="explicit-any", path=path, summary=f"fix {path}",
                   estimated_lines=lines, severity=sev, symbol=symbol)


def test_batches_group_by_path() -> None:
    batches = plan_batches([_f("a.ts"), _f("a.ts"), _f("b.ts")])
    paths = {tuple(b.paths) for b in batches}
    assert ("a.ts",) in paths and ("b.ts",) in paths


def test_batch_splits_when_over_line_cap() -> None:
    findings = [_f("a.ts", lines=80), _f("a.ts", lines=80), _f("a.ts", lines=80)]
    batches = plan_batches(findings, max_lines=150)
    # 3x80 lines can't fit in one 150-line PR -> at least 2 batches, none exceeding the cap
    assert len(batches) >= 2
    assert all(b.estimated_lines <= 150 for b in batches)


def test_batch_splits_when_over_finding_cap() -> None:
    findings = [_f("a.ts", lines=1) for _ in range(20)]
    batches = plan_batches(findings, max_lines=1000, max_findings=8)
    assert all(len(b.findings) <= 8 for b in batches)
    assert sum(len(b.findings) for b in batches) == 20


def test_group_by_symbol() -> None:
    findings = [_f("a.ts", symbol="parseUser"), _f("a.ts", symbol="parseUser"),
                _f("a.ts", symbol="renderRow")]
    batches = plan_batches(findings, group_by="symbol")
    assert len(batches) == 2


def test_high_severity_leads_batch() -> None:
    findings = [_f("a.ts", sev=Severity.LOW), _f("a.ts", sev=Severity.CRITICAL)]
    [batch] = plan_batches(findings)
    assert batch.findings[0].severity is Severity.CRITICAL


# ----- run_code_work driver --------------------------------------------------------


@dataclass
class _FakeSurgeon:
    """Returns a canned proposal; lets us drive the gate without git/Claude."""

    changed_lines: int = 30
    checks_passed: bool = True
    rejected: str = ""

    async def run(self, spec) -> ChangeProposal:  # noqa: ANN001
        return ChangeProposal(
            branch=f"agent/{spec.title[:8]}",
            base_branch=spec.base_branch,
            title=spec.title,
            body="body",
            changed_lines=self.changed_lines,
            checks_passed=self.checks_passed,
            rejected_reason=self.rejected,
        )


def _run(coro):
    return asyncio.run(coro)


def test_driver_opens_draft_pr_for_good_proposal() -> None:
    guard = PolicyGuard(config=PolicyConfig())
    opened: list[str] = []
    _run(run_code_work(
        guard=guard, surgeon=_FakeSurgeon(), batches=plan_batches([_f("a.ts")]),
        repo_path="/repo", base_branch="main", run_checks=["tsc"], actor="agent.types",
        open_pr=lambda p: opened.append(p.branch) or "https://pr/1",
    ))
    pr_actions = [r for r in guard.audit_log if r.request.action is ActionType.OPEN_DRAFT_PR]
    assert len(pr_actions) == 1 and pr_actions[0].executed
    assert opened  # executor actually ran


def test_driver_skips_oversized_proposal() -> None:
    guard = PolicyGuard(config=PolicyConfig())
    _run(run_code_work(
        guard=guard, surgeon=_FakeSurgeon(rejected="diff too large"),
        batches=plan_batches([_f("a.ts")]), repo_path="/repo", base_branch="main",
        run_checks=["tsc"], actor="agent.types",
    ))
    rec = guard.audit_log[-1]
    assert not rec.allowed and "large" in rec.reason
    assert not rec.executed  # never opened


def test_driver_skips_when_checks_fail() -> None:
    guard = PolicyGuard(config=PolicyConfig())
    _run(run_code_work(
        guard=guard, surgeon=_FakeSurgeon(checks_passed=False),
        batches=plan_batches([_f("a.ts")]), repo_path="/repo", base_branch="main",
        run_checks=["tsc"], actor="agent.types",
    ))
    assert not guard.audit_log[-1].executed
