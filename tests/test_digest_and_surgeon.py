"""Tests for the morning digest renderer and the Code Surgeon's pure helpers."""

from __future__ import annotations

from overnight_eng.models import ActionRequest, ActionType
from overnight_eng.reporting.digest import render_digest
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.workers.code_surgeon import ChangeProposal, _slug


def _build_log() -> list:
    guard = PolicyGuard(config=PolicyConfig())
    guard.guard(
        ActionRequest(ActionType.CREATE_ISSUE, actor="triage", summary="DB timeout in checkout"),
        lambda r: "https://github.com/o/r/issues/12",
    )
    guard.guard(
        ActionRequest(ActionType.MERGE, actor="pr", summary="merge #4", branch="agent/x"),
        lambda r: "should not run",
    )
    return guard.audit_log


def test_digest_reports_done_and_blocked() -> None:
    md = render_digest(_build_log())
    assert "Overnight Engineering" in md
    assert "Issues filed" in md
    assert "issues/12" in md
    assert "Skipped by policy" in md
    assert "merge" in md.lower()


def test_digest_quiet_night() -> None:
    assert "Quiet night" in render_digest([])


def test_digest_dry_run_section() -> None:
    guard = PolicyGuard(config=PolicyConfig(dry_run=True))
    guard.guard(
        ActionRequest(ActionType.OPEN_DRAFT_PR, actor="ts", summary="remove any in api.ts",
                      branch="agent/types", target_branch="main"),
        lambda r: "x",
    )
    md = render_digest(guard.audit_log)
    assert "Proposed (dry-run" in md
    assert "open_draft_pr" in md


def test_slug_is_branch_safe() -> None:
    assert _slug("Remove `any` from API client!!") == "remove-any-from-api-client"


def test_change_proposal_ok_logic() -> None:
    good = ChangeProposal("agent/x", "main", "t", "b", changed_lines=20, checks_passed=True)
    assert good.ok
    too_big = ChangeProposal("agent/x", "main", "t", "b", 999, True, rejected_reason="too large")
    assert not too_big.ok
    failed = ChangeProposal("agent/x", "main", "t", "b", 20, checks_passed=False)
    assert not failed.ok
