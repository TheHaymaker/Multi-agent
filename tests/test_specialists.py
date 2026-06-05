"""Tests for the specialist detectors + the PR coordinator planner (pure cores)."""

from __future__ import annotations

import json

from overnight_eng.models import ActionType, Severity
from overnight_eng.specialists.code_quality import parse_ruff
from overnight_eng.specialists.coverage import parse_coverage
from overnight_eng.specialists.performance import detect_regressions
from overnight_eng.specialists.pr_coordinator import (
    PullRequest,
    coordinate_prs,
    plan_pr_actions,
)
from overnight_eng.specialists.typescript_types import parse_eslint, parse_tsc
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard


# ----- TypeScript type hygiene -----------------------------------------------------


def test_parse_eslint_picks_only_type_rules() -> None:
    report = json.dumps([
        {"filePath": "src/api.ts", "messages": [
            {"ruleId": "@typescript-eslint/no-explicit-any", "line": 10, "message": "Unexpected any"},
            {"ruleId": "@typescript-eslint/no-unsafe-assignment", "line": 12,
             "message": "Unsafe assignment to 'user'"},
            {"ruleId": "semi", "line": 3, "message": "Missing semicolon"},  # ignored
        ]},
    ])
    findings = parse_eslint(report)
    kinds = {f.kind for f in findings}
    assert kinds == {"explicit-any", "unsafe-type"}
    assert any(f.symbol == "user" for f in findings)
    assert any(f.severity is Severity.HIGH for f in findings)  # unsafe-assignment


def test_parse_tsc_implicit_any() -> None:
    out = (
        "src/util.ts(7,12): error TS7006: Parameter 'x' implicitly has an 'any' type.\n"
        "src/util.ts(9,1): error TS2304: Cannot find name 'foo'.\n"  # not implicit-any -> ignored
    )
    findings = parse_tsc(out)
    assert len(findings) == 1
    assert findings[0].kind == "implicit-any" and findings[0].path == "src/util.ts"


def test_parse_eslint_handles_garbage() -> None:
    assert parse_eslint("not json") == []


# ----- code quality (ruff) ---------------------------------------------------------


def test_parse_ruff_severity_and_fixable() -> None:
    report = json.dumps([
        {"code": "F401", "filename": "a.py", "location": {"row": 1},
         "message": "imported but unused", "fix": {"applicability": "safe"}},
        {"code": "E501", "filename": "a.py", "location": {"row": 5}, "message": "line too long"},
    ])
    findings = parse_ruff(report)
    by_code = {f.summary.split()[0]: f for f in findings}
    assert by_code["F401"].severity is Severity.HIGH      # pyflakes = bug-class
    assert by_code["E501"].severity is Severity.LOW       # style
    assert by_code["F401"].estimated_lines < by_code["E501"].estimated_lines  # autofix smaller


# ----- coverage --------------------------------------------------------------------


def test_parse_coverage_flags_low_files_only() -> None:
    report = json.dumps({"files": {
        "well_tested.py": {"summary": {"percent_covered": 95.0, "missing_lines": 2}},
        "under.py": {"summary": {"percent_covered": 40.0, "missing_lines": 30},
                     "missing_lines": [10, 11, 12]},
    }})
    findings = parse_coverage(report, threshold=80.0)
    assert len(findings) == 1
    assert findings[0].path == "under.py" and findings[0].severity is Severity.HIGH


# ----- performance -----------------------------------------------------------------


def test_detect_regressions_above_threshold() -> None:
    bench = json.dumps({"benchmarks": [
        {"fullname": "tests/test_perf.py::test_hot", "stats": {"mean": 0.30}},
        {"fullname": "tests/test_perf.py::test_ok", "stats": {"mean": 0.10}},
    ]})
    baseline = {"tests/test_perf.py::test_hot": 0.10, "tests/test_perf.py::test_ok": 0.099}
    findings = detect_regressions(bench, baseline, threshold=0.20)
    assert len(findings) == 1
    assert "test_hot" in findings[0].summary and findings[0].severity is Severity.HIGH


def test_detect_regressions_ignores_new_benchmarks() -> None:
    bench = json.dumps({"benchmarks": [{"fullname": "new::bench", "stats": {"mean": 1.0}}]})
    assert detect_regressions(bench, {}) == []


# ----- PR coordinator planner ------------------------------------------------------


def test_rebases_stale_agent_branch() -> None:
    pr = PullRequest(number=1, branch="agent/fix-types", base="main", behind_by=4)
    actions = plan_pr_actions(pr)
    assert any(a.action is ActionType.REBASE_AGENT_BRANCH for a in actions)


def test_does_not_rebase_into_conflict() -> None:
    pr = PullRequest(number=2, branch="agent/x", base="main", behind_by=4, has_conflicts=True)
    actions = plan_pr_actions(pr)
    assert all(a.action is not ActionType.REBASE_AGENT_BRANCH for a in actions)
    assert any(a.action is ActionType.COMMENT for a in actions)


def test_never_touches_non_agent_branch() -> None:
    pr = PullRequest(number=3, branch="feature/theirs", base="main", behind_by=5,
                     unresolved_comments=["please fix"])
    actions = plan_pr_actions(pr)
    # only an advisory comment, never a rebase or PR update
    assert {a.action for a in actions} == {ActionType.COMMENT}


def test_addresses_review_comments_on_agent_pr() -> None:
    pr = PullRequest(number=4, branch="agent/x", base="main",
                     unresolved_comments=["use a real type", "add a test"])
    actions = plan_pr_actions(pr)
    types = {a.action for a in actions}
    assert ActionType.COMMENT in types and ActionType.UPDATE_DRAFT_PR in types


def test_coordinate_prs_routes_through_guard_and_denies_nothing_unsafe() -> None:
    guard = PolicyGuard(config=PolicyConfig())
    prs = [
        PullRequest(number=1, branch="agent/a", base="main", behind_by=2),
        PullRequest(number=2, branch="feature/theirs", base="main",
                    unresolved_comments=["x"]),
    ]
    coordinate_prs(guard, prs)
    # every executed action was allowed (the planner never emits a denied action)
    assert all(r.allowed for r in guard.audit_log)
    assert any(r.request.action is ActionType.REBASE_AGENT_BRANCH and r.executed
               for r in guard.audit_log)
