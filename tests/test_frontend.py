"""Tests: Code Quality + Performance specialists on TypeScript/React/Next.js inputs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from overnight_eng.models import ActionType, Severity
from overnight_eng.scheduler.code_sweep import run_code_sweep
from overnight_eng.specialists.code_quality import parse_eslint as parse_eslint_quality
from overnight_eng.specialists.performance import (
    DEFAULT_LH_BUDGETS,
    parse_lighthouse,
    parse_next_build,
)
from overnight_eng.tools.forge import build_forge
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.workers.code_surgeon import ChangeProposal


# ----- Code Quality: ESLint React/Next rules ---------------------------------------


def test_eslint_quality_picks_react_next_rules_not_type_rules() -> None:
    report = json.dumps([{"filePath": "app/page.tsx", "messages": [
        {"ruleId": "react-hooks/exhaustive-deps", "line": 12, "severity": 1,
         "message": "React Hook useEffect has a missing dependency: 'user'"},
        {"ruleId": "@next/next/no-img-element", "line": 20, "severity": 1, "message": "Use next/image"},
        {"ruleId": "@typescript-eslint/no-explicit-any", "line": 4, "severity": 2, "message": "any"},
    ]}])
    findings = parse_eslint_quality(report)
    rules = {f.summary.split()[0] for f in findings}
    assert "react-hooks/exhaustive-deps" in rules        # quality rule kept
    assert "@next/next/no-img-element" in rules
    assert "@typescript-eslint/no-explicit-any" not in rules  # owned by typescript_types
    deps = next(f for f in findings if "exhaustive-deps" in f.summary)
    assert deps.severity is Severity.HIGH and deps.symbol == "user"


def test_eslint_quality_skips_unknown_warnings_keeps_unknown_errors() -> None:
    report = json.dumps([{"filePath": "a.ts", "messages": [
        {"ruleId": "some-style-rule", "line": 1, "severity": 1, "message": "nit"},   # dropped
        {"ruleId": "some-bug-rule", "line": 2, "severity": 2, "message": "boom"},     # kept
    ]}])
    findings = parse_eslint_quality(report)
    assert [f.summary.split()[0] for f in findings] == ["some-bug-rule"]


# ----- Performance: Next.js bundle size --------------------------------------------


def test_next_build_regression_from_json() -> None:
    data = json.dumps({"routes": {"/": 90.0, "/dashboard": 250.0}})
    baseline = {"/": 88.0, "/dashboard": 150.0}
    findings = parse_next_build(data, baseline, threshold=0.15)
    # "/" grew 2% (under threshold); "/dashboard" grew 66% -> flagged HIGH (>40%)
    assert len(findings) == 1
    assert "/dashboard" in findings[0].summary and findings[0].severity is Severity.HIGH
    assert findings[0].kind == "bundle-regression"


def test_next_build_parses_table_text_and_budget() -> None:
    text = (
        "Route (app)                              Size     First Load JS\n"
        "┌ ○ /                                    1.2 kB         85.3 kB\n"
        "├ ○ /heavy                               5 kB           420 kB\n"
    )
    findings = parse_next_build(text, baseline={}, budget_kb=300.0)
    assert len(findings) == 1
    assert findings[0].kind == "bundle-budget" and "/heavy" in findings[0].summary


# ----- Performance: Lighthouse / Core Web Vitals -----------------------------------


def test_lighthouse_flags_score_and_vitals() -> None:
    lhr = json.dumps({
        "finalUrl": "https://app/",
        "categories": {"performance": {"score": 0.72}},
        "audits": {
            "largest-contentful-paint": {"numericValue": 4200},
            "total-blocking-time": {"numericValue": 120},      # under budget -> not flagged
            "cumulative-layout-shift": {"numericValue": 0.30},
        },
    })
    findings = parse_lighthouse(lhr)
    metrics = {f.symbol for f in findings}
    assert "performance" in metrics                 # 0.72 < 0.90
    assert "largest-contentful-paint" in metrics    # 4200 > 2500
    assert "cumulative-layout-shift" in metrics     # 0.30 > 0.10
    assert "total-blocking-time" not in metrics     # within budget
    assert DEFAULT_LH_BUDGETS["lcp_ms"] == 2500.0


def test_lighthouse_clean_run_is_quiet() -> None:
    lhr = json.dumps({
        "categories": {"performance": {"score": 0.98}},
        "audits": {"largest-contentful-paint": {"numericValue": 1200},
                   "total-blocking-time": {"numericValue": 50},
                   "cumulative-layout-shift": {"numericValue": 0.02}},
    })
    assert parse_lighthouse(lhr) == []


# ----- end-to-end: a Next.js/React project sweep -----------------------------------


@dataclass
class _FakeSurgeon:
    async def run(self, spec):  # noqa: ANN001
        return ChangeProposal(branch=f"agent/{abs(hash(spec.title)) % 9999}",
                              base_branch=spec.base_branch, title=spec.title, body="b",
                              changed_lines=20, checks_passed=True)


@dataclass
class _RT:
    guard: PolicyGuard
    surgeon: _FakeSurgeon
    forge: object


def test_nextjs_sweep_opens_prs_for_code_and_files_issues_for_perf() -> None:
    calls: list[tuple[str, dict]] = []
    rt = _RT(PolicyGuard(config=PolicyConfig()), _FakeSurgeon(),
             build_forge("github", lambda t, k: calls.append((t, k)) or "ok"))

    reports = {
        "eslint": json.dumps([{"filePath": "app/page.tsx", "messages": [
            {"ruleId": "react-hooks/exhaustive-deps", "line": 5, "severity": 2, "message": "missing dep 'x'"},
            {"ruleId": "@typescript-eslint/no-explicit-any", "line": 8, "severity": 2, "message": "any"},
        ]}]),
        "next_build": (json.dumps({"routes": {"/dashboard": 400.0}}), {"/dashboard": 150.0}),
        "lighthouse": json.dumps({"categories": {"performance": {"score": 0.5}},
                                  "audits": {"largest-contentful-paint": {"numericValue": 5000}}}),
    }
    asyncio.run(run_code_sweep(rt, repo_path="o/r", base_branch="main", reports=reports))

    actions = [r.request.action for r in rt.guard.audit_log]
    # code fixes -> draft PRs (react-hooks via code_quality, any via typescript_types)
    assert actions.count(ActionType.OPEN_DRAFT_PR) == 2
    # perf (bundle + lighthouse) -> filed issues, not PRs
    assert ActionType.CREATE_ISSUE in actions
    tools = [c[0] for c in calls]
    assert "create_pull_request" in tools and "create_issue" in tools
