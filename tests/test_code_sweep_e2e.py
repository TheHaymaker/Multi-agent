"""End-to-end test of the code-improvement sweep with a fake surgeon + forge.

Proves the whole vertical: tool reports -> findings -> small batches -> Code Surgeon ->
PolicyGuard-gated draft PRs, plus PR coordination — all without git or a live forge.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from overnight_eng.models import ActionType
from overnight_eng.scheduler.code_sweep import run_code_sweep
from overnight_eng.specialists.pr_coordinator import PullRequest
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.workers.code_surgeon import ChangeProposal


@dataclass
class _FakeSurgeon:
    changed_lines: int = 25

    async def run(self, spec):  # noqa: ANN001
        # Always produces an agent-owned branch so PolicyGuard permits the draft PR.
        return ChangeProposal(
            branch=f"agent/{spec.branch_prefix}-{abs(hash(spec.title)) % 9999}",
            base_branch=spec.base_branch, title=spec.title, body="body",
            changed_lines=self.changed_lines, checks_passed=True,
        )


@dataclass
class _RT:
    guard: PolicyGuard
    surgeon: _FakeSurgeon = field(default_factory=_FakeSurgeon)


def _reports() -> dict:
    return {
        "eslint": json.dumps([{"filePath": "src/api.ts", "messages": [
            {"ruleId": "@typescript-eslint/no-explicit-any", "line": 3, "message": "any"},
        ]}]),
        "ruff": json.dumps([
            {"code": "F401", "filename": "a.py", "location": {"row": 1}, "message": "unused"},
        ]),
        "coverage": json.dumps({"files": {
            "under.py": {"summary": {"percent_covered": 30.0, "missing_lines": 12},
                         "missing_lines": [1, 2, 3]},
        }}),
    }


def test_code_sweep_opens_small_draft_prs_and_rebases() -> None:
    rt = _RT(guard=PolicyGuard(config=PolicyConfig()))
    opened: list[str] = []
    prs = [PullRequest(number=9, branch="agent/old", base="main", behind_by=3)]

    digest = asyncio.run(run_code_sweep(
        rt, repo_path="/repo", base_branch="main", reports=_reports(), prs=prs,
        open_pr=lambda p: opened.append(p.title) or "https://pr",
    ))

    actions = [r.request.action for r in rt.guard.audit_log]
    # one draft PR per specialist with findings (ts, ruff, coverage) = 3
    assert actions.count(ActionType.OPEN_DRAFT_PR) == 3
    assert all(
        r.executed for r in rt.guard.audit_log if r.request.action is ActionType.OPEN_DRAFT_PR
    )
    # the stale agent PR got rebased
    assert ActionType.REBASE_AGENT_BRANCH in actions
    assert "Overnight Engineering" in digest
    assert len(opened) == 3


def test_oversized_changes_are_not_opened() -> None:
    rt = _RT(guard=PolicyGuard(config=PolicyConfig()), surgeon=_FakeSurgeon(changed_lines=999))
    asyncio.run(run_code_sweep(rt, repo_path="/repo", reports=_reports(), max_lines=150))
    prs = [r for r in rt.guard.audit_log if r.request.action is ActionType.OPEN_DRAFT_PR]
    assert prs and all(not r.executed for r in prs)  # all rejected as too large
    assert all("large" in r.reason for r in prs)


def test_empty_reports_quiet_night() -> None:
    rt = _RT(guard=PolicyGuard(config=PolicyConfig()))
    digest = asyncio.run(run_code_sweep(rt, repo_path="/repo", reports={}))
    assert "Quiet night" in digest
