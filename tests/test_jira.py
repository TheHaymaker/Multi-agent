"""Tests for the Jira/backlog reconciler — cross-system de-dup + priority proposals."""

from __future__ import annotations

from overnight_eng.models import ActionType, Severity
from overnight_eng.specialists.jira_backlog import (
    FiledItem,
    JiraIssue,
    find_match,
    fingerprint_of,
    reconcile,
    reconcile_backlog,
    severity_to_priority,
)
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard


def test_severity_priority_mapping() -> None:
    assert severity_to_priority(Severity.CRITICAL) == "Highest"
    assert severity_to_priority(Severity.LOW) == "Low"


def test_fingerprint_from_label() -> None:
    issue = JiraIssue(key="P-1", summary="x", labels=["overnight", "fp:abc123"])
    assert fingerprint_of(issue) == "abc123"


def test_match_by_fingerprint() -> None:
    item = FiledItem("FP9", "DB timeout", Severity.HIGH, "gh#1")
    issues = [JiraIssue(key="P-2", summary="unrelated", labels=["fp:FP9"])]
    assert find_match(item, issues).key == "P-2"


def test_match_by_normalized_title_when_open() -> None:
    item = FiledItem("", "Timeout after 5000ms", Severity.HIGH, "gh#1")
    issues = [JiraIssue(key="P-3", summary="Timeout after 9123ms", status="Open")]
    assert find_match(item, issues).key == "P-3"


def test_no_match_against_closed_ticket() -> None:
    item = FiledItem("", "Timeout after 5000ms", Severity.HIGH, "gh#1")
    issues = [JiraIssue(key="P-4", summary="Timeout after 9123ms", status="Done")]
    assert find_match(item, issues) is None


def test_reconcile_creates_tracking_ticket_for_untracked() -> None:
    filed = [FiledItem("FP1", "boom", Severity.CRITICAL, "gh#1")]
    actions = reconcile(filed, [], project_key="OPS")
    assert len(actions) == 1
    a = actions[0]
    assert a.action is ActionType.CREATE_ISSUE
    assert "fp:FP1" in a.payload["labels"] and a.payload["priority"] == "Highest"


def test_reconcile_does_not_duplicate_tracked() -> None:
    filed = [FiledItem("FP1", "boom", Severity.HIGH, "gh#1")]
    issues = [JiraIssue(key="P-1", summary="boom", priority="High", labels=["fp:FP1"])]
    assert reconcile(filed, issues) == []


def test_reconcile_suggests_priority_when_mismatched() -> None:
    filed = [FiledItem("FP1", "boom", Severity.CRITICAL, "gh#1")]
    issues = [JiraIssue(key="P-1", summary="boom", priority="Low", labels=["fp:FP1"])]
    [action] = reconcile(filed, issues)
    assert action.action is ActionType.COMMENT
    assert action.payload["suggested_priority"] == "Highest"


def test_reconcile_backlog_is_policy_gated() -> None:
    guard = PolicyGuard(config=PolicyConfig())
    filed = [FiledItem("FP1", "boom", Severity.HIGH, "gh#1")]
    reconcile_backlog(guard, filed, [], project_key="OPS")
    assert guard.audit_log and all(r.allowed for r in guard.audit_log)
    assert guard.audit_log[0].request.action is ActionType.CREATE_ISSUE
