"""Tests for the propose-only PolicyGuard — the security-critical core.

These run with stdlib + pytest only (no agent runtime, no network). The whole point of
routing every write through one gate is that the safety policy is verifiable here.
"""

from __future__ import annotations

import pytest

from overnight_eng.models import ActionRequest, ActionType
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard


@pytest.fixture
def guard() -> PolicyGuard:
    return PolicyGuard(config=PolicyConfig())


def _req(action: ActionType, **kw) -> ActionRequest:
    kw.setdefault("actor", "test-agent")
    kw.setdefault("summary", "test action")
    return ActionRequest(action=action, **kw)


# ----- allowed actions -------------------------------------------------------------


def test_create_issue_allowed(guard: PolicyGuard) -> None:
    assert guard.evaluate(_req(ActionType.CREATE_ISSUE)).allowed


def test_comment_allowed(guard: PolicyGuard) -> None:
    assert guard.evaluate(_req(ActionType.COMMENT)).allowed


def test_draft_pr_from_agent_branch_to_main_allowed(guard: PolicyGuard) -> None:
    d = guard.evaluate(
        _req(ActionType.OPEN_DRAFT_PR, branch="agent/fix-types-123", target_branch="main")
    )
    assert d.allowed, d.reason


def test_rebase_agent_branch_onto_main_allowed(guard: PolicyGuard) -> None:
    d = guard.evaluate(
        _req(ActionType.REBASE_AGENT_BRANCH, branch="overnight/perf-sweep", target_branch="main")
    )
    assert d.allowed, d.reason


def test_push_to_agent_branch_allowed(guard: PolicyGuard) -> None:
    assert guard.evaluate(_req(ActionType.PUSH_AGENT_BRANCH, branch="bot/coverage")).allowed


# ----- denied actions --------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        ActionType.MERGE,
        ActionType.FORCE_PUSH,
        ActionType.PUSH_PROTECTED,
        ActionType.CLOSE_OTHERS,
        ActionType.DELETE_BRANCH,
    ],
)
def test_categorically_denied(guard: PolicyGuard, action: ActionType) -> None:
    d = guard.evaluate(_req(action, branch="agent/whatever", target_branch="main"))
    assert not d.allowed
    assert "denied" in d.reason


def test_push_to_protected_branch_denied(guard: PolicyGuard) -> None:
    d = guard.evaluate(_req(ActionType.PUSH_AGENT_BRANCH, branch="main"))
    assert not d.allowed
    assert "protected" in d.reason


def test_push_to_release_glob_denied(guard: PolicyGuard) -> None:
    assert not guard.evaluate(_req(ActionType.PUSH_AGENT_BRANCH, branch="release/2.0")).allowed


def test_non_agent_branch_denied(guard: PolicyGuard) -> None:
    d = guard.evaluate(_req(ActionType.PUSH_AGENT_BRANCH, branch="feature/someone-elses"))
    assert not d.allowed
    assert "not agent-owned" in d.reason


def test_branch_scoped_action_without_branch_denied(guard: PolicyGuard) -> None:
    assert not guard.evaluate(_req(ActionType.OPEN_DRAFT_PR)).allowed


def test_unknown_action_fails_closed() -> None:
    # Simulate a value not in the allow/deny sets by monkey-constructing.
    guard = PolicyGuard()
    req = _req(ActionType.CREATE_ISSUE)
    object.__setattr__(req, "action", "totally_unknown")  # type: ignore[arg-type]
    d = guard.evaluate(req)
    assert not d.allowed


# ----- execution + dry-run ---------------------------------------------------------


def test_execute_runs_only_when_allowed(guard: PolicyGuard) -> None:
    calls: list[str] = []

    def executor(req: ActionRequest) -> str:
        calls.append(req.action.value)
        return "ok"

    rec = guard.guard(_req(ActionType.CREATE_ISSUE), executor)
    assert rec.executed and rec.result == "ok"
    assert calls == ["create_issue"]


def test_denied_action_never_executes(guard: PolicyGuard) -> None:
    calls: list[str] = []
    rec = guard.guard(_req(ActionType.MERGE, branch="agent/x"), lambda r: calls.append("x") or "x")
    assert not rec.executed
    assert calls == []


def test_dry_run_evaluates_but_does_not_execute() -> None:
    guard = PolicyGuard(config=PolicyConfig(dry_run=True))
    calls: list[str] = []
    rec = guard.guard(_req(ActionType.CREATE_ISSUE), lambda r: calls.append("x") or "x")
    assert rec.allowed and not rec.executed and rec.dry_run
    assert calls == []  # zero side effects during a dry-run sweep


def test_audit_log_and_summary(guard: PolicyGuard) -> None:
    guard.guard(_req(ActionType.CREATE_ISSUE), lambda r: "ok")
    guard.guard(_req(ActionType.MERGE, branch="agent/x"), lambda r: "ok")
    s = guard.summary()
    assert s == {"total": 2, "allowed": 1, "executed": 1, "denied": 1}


def test_executor_error_recorded_not_raised(guard: PolicyGuard) -> None:
    def boom(_req: ActionRequest) -> str:
        raise RuntimeError("mcp down")

    rec = guard.guard(_req(ActionType.CREATE_ISSUE), boom)
    assert rec.allowed and not rec.executed
    assert "mcp down" in rec.result
