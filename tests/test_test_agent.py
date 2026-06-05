"""Tests: Test & Coverage specialist on Jest/Vitest (Istanbul) + Playwright inputs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from overnight_eng.models import ActionType, Severity
from overnight_eng.scheduler.code_sweep import run_code_sweep
from overnight_eng.specialists.coverage import parse_js_coverage, parse_playwright
from overnight_eng.tools.forge import build_forge
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.workers.code_surgeon import ChangeProposal


# ----- Jest/Vitest coverage (Istanbul) ---------------------------------------------


def test_js_coverage_summary_shape() -> None:
    report = json.dumps({
        "total": {"lines": {"total": 100, "covered": 50, "pct": 50.0}},
        "src/well.ts": {"lines": {"total": 100, "covered": 98, "pct": 98.0}},
        "src/under.ts": {"lines": {"total": 100, "covered": 40, "pct": 40.0}},
    })
    findings = parse_js_coverage(report, threshold=80.0)
    assert len(findings) == 1
    assert findings[0].path == "src/under.ts" and findings[0].kind == "coverage-gap"
    assert findings[0].severity is Severity.HIGH  # 40 < 80-20


def test_js_coverage_final_shape_extracts_missing_lines() -> None:
    report = json.dumps({
        "src/util.ts": {
            "path": "src/util.ts",
            "statementMap": {
                "0": {"start": {"line": 3}}, "1": {"start": {"line": 4}},
                "2": {"start": {"line": 5}}, "3": {"start": {"line": 6}},
            },
            "s": {"0": 1, "1": 0, "2": 0, "3": 0},  # 1/4 covered = 25%
        },
    })
    findings = parse_js_coverage(report, threshold=80.0)
    assert len(findings) == 1
    assert "25%" in findings[0].summary
    assert "4, 5, 6" in findings[0].detail  # uncovered statement lines


def test_js_coverage_ignores_well_covered() -> None:
    report = json.dumps({"total": {"lines": {"pct": 95.0}},
                         "a.ts": {"lines": {"total": 100, "covered": 99, "pct": 99.0}}})
    assert parse_js_coverage(report) == []


# ----- Playwright E2E ---------------------------------------------------------------


def test_playwright_classifies_failing_flaky_skipped() -> None:
    report = json.dumps({"suites": [{
        "title": "checkout.spec.ts", "file": "e2e/checkout.spec.ts",
        "specs": [
            {"title": "completes purchase", "tests": [
                {"status": "unexpected", "results": [{"status": "failed", "retry": 0}]}]},
            {"title": "applies coupon", "tests": [
                {"status": "flaky", "results": [{"status": "failed", "retry": 0},
                                                {"status": "passed", "retry": 1}]}]},
            {"title": "guest checkout", "tests": [{"status": "skipped", "results": []}]},
            {"title": "happy path", "tests": [{"status": "expected", "results": []}]},
        ],
    }]})
    findings = parse_playwright(report)
    kinds = {f.kind for f in findings}
    assert kinds == {"failing-test", "flaky-test", "skipped-test"}  # 'expected' excluded
    flaky = next(f for f in findings if f.kind == "flaky-test")
    assert "retry 1" in flaky.summary and flaky.severity is Severity.HIGH


def test_playwright_walks_nested_suites() -> None:
    report = json.dumps({"suites": [{
        "title": "outer", "file": "e2e/a.spec.ts", "specs": [],
        "suites": [{"title": "inner", "specs": [
            {"title": "nested test", "tests": [{"status": "unexpected", "results": []}]}]}],
    }]})
    findings = parse_playwright(report)
    assert len(findings) == 1 and findings[0].path == "e2e/a.spec.ts"


# ----- end-to-end -------------------------------------------------------------------


@dataclass
class _FakeSurgeon:
    async def run(self, spec):  # noqa: ANN001
        return ChangeProposal(branch=f"agent/{abs(hash(spec.title)) % 9999}",
                              base_branch=spec.base_branch, title=spec.title, body="b",
                              changed_lines=30, checks_passed=True)


@dataclass
class _RT:
    guard: PolicyGuard
    surgeon: _FakeSurgeon
    forge: object


def test_sweep_adds_tests_for_coverage_and_files_issues_for_playwright() -> None:
    calls: list[tuple[str, dict]] = []
    rt = _RT(PolicyGuard(config=PolicyConfig()), _FakeSurgeon(),
             build_forge("github", lambda t, k: calls.append((t, k)) or "ok"))

    reports = {
        "js_coverage": json.dumps({"total": {"lines": {"pct": 40.0}},
                                   "src/cart.ts": {"lines": {"total": 80, "covered": 20, "pct": 25.0}}}),
        "playwright": json.dumps({"suites": [{"file": "e2e/x.spec.ts", "specs": [
            {"title": "flaky one", "tests": [{"status": "flaky", "results": [{"retry": 1}]}]}]}]}),
    }
    asyncio.run(run_code_sweep(rt, repo_path="o/r", base_branch="main", reports=reports))

    actions = [r.request.action for r in rt.guard.audit_log]
    assert ActionType.OPEN_DRAFT_PR in actions   # coverage gap -> add tests via draft PR
    assert ActionType.CREATE_ISSUE in actions    # flaky E2E -> filed issue, not auto-edited
    tools = [c[0] for c in calls]
    assert "create_pull_request" in tools and "create_issue" in tools
