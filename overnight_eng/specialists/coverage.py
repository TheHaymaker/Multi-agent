"""Test Quality & Coverage specialist — Python (coverage.py) and JS/TS (Jest/Vitest/Playwright).

Coverage gaps become "add tests for X" draft PRs:
  * :func:`parse_coverage` — Python ``coverage.py`` JSON.
  * :func:`parse_js_coverage` — Istanbul output shared by Jest and Vitest, in either the
    ``coverage-summary.json`` (json-summary) or ``coverage-final.json`` (full) shape.

End-to-end test health becomes issues (a failing E2E may be a real product bug, so we report
with analysis rather than auto-editing the test):
  * :func:`parse_playwright` — Playwright JSON report → failing / flaky / skipped tests.

(A mutation-testing pass via ``mutmut`` / ``stryker`` plugs in the same way: survivors -> Findings.)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["pytest -q", "coverage run -m pytest && coverage json -o coverage.json"]
# Jest or Vitest with Istanbul coverage; the agent's new tests must pass + lift coverage.
JS_CHECKS = ["npx --yes vitest run --coverage", "npx --yes jest --coverage"]
PLAYWRIGHT_CHECKS = ["npx --yes playwright test --reporter=line"]


def _gap_finding(path: str, pct: float, missing: int, missing_lines: list[int],
                 *, threshold: float) -> Finding:
    sev = Severity.HIGH if pct < threshold - 20 else Severity.MEDIUM
    return Finding(
        kind="coverage-gap",
        path=path,
        summary=f"{path} at {pct:.0f}% ({missing} lines uncovered)",
        detail="Add focused tests for the uncovered branches/lines: "
               + ", ".join(map(str, missing_lines[:15])),
        severity=sev,
        # test-writing scales with the gap, but stays capped per PR by the planner
        estimated_lines=min(missing * 2, 60),
    )


def parse_coverage(report_json: str, *, threshold: float = 80.0, min_missing: int = 3) -> list[Finding]:
    """Flag files below ``threshold`` percent covered with at least ``min_missing`` lines uncovered."""
    try:
        data = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for path, info in (data.get("files") or {}).items():
        summary = info.get("summary", {})
        pct = float(summary.get("percent_covered", 100.0))
        missing = int(summary.get("missing_lines", 0))
        if pct >= threshold or missing < min_missing:
            continue
        findings.append(_gap_finding(path, pct, missing, info.get("missing_lines") or [],
                                     threshold=threshold))
    return findings


def parse_js_coverage(report_json: str, *, threshold: float = 80.0, min_missing: int = 3) -> list[Finding]:
    """Parse Istanbul coverage (Jest/Vitest) — both the summary and the full shapes."""
    try:
        data = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    findings: list[Finding] = []

    # coverage-summary.json: {"total": {...}, "<file>": {"lines": {pct, covered, total}}}
    total = data.get("total")
    if isinstance(total, dict) and "lines" in total:
        for path, info in data.items():
            if path == "total" or not isinstance(info, dict):
                continue
            lines = info.get("lines", {})
            pct = float(lines.get("pct", 100.0))
            covered, ltotal = int(lines.get("covered", 0)), int(lines.get("total", 0))
            missing = ltotal - covered
            if pct >= threshold or missing < min_missing:
                continue
            findings.append(_gap_finding(path, pct, missing, [], threshold=threshold))
        return findings

    # coverage-final.json: {"<file>": {"statementMap": {...}, "s": {id: hits}}}
    for path, info in data.items():
        if not isinstance(info, dict) or "statementMap" not in info:
            continue
        s = info.get("s", {})
        smap = info.get("statementMap", {})
        stotal = len(s)
        covered = sum(1 for hit in s.values() if hit)
        missing = stotal - covered
        pct = 100.0 if stotal == 0 else covered / stotal * 100
        if pct >= threshold or missing < min_missing:
            continue
        miss_lines = sorted({
            smap[k]["start"]["line"] for k, hit in s.items() if not hit and k in smap
        })
        findings.append(_gap_finding(path, pct, missing, miss_lines, threshold=threshold))
    return findings


# Playwright test status -> (kind, severity, advice).
_PW_STATUS = {
    "unexpected": ("failing-test", Severity.HIGH,
                   "E2E test is failing. Investigate whether it's a real product regression "
                   "(file a bug) or a stale test that needs updating — do NOT edit the test to "
                   "force a pass."),
    "flaky": ("flaky-test", Severity.HIGH,
              "E2E test is flaky (passed only on retry). Stabilize it: replace arbitrary waits "
              "with web-first assertions / auto-waiting locators and remove timing races."),
    "skipped": ("skipped-test", Severity.LOW,
                "E2E test is skipped. Re-enable it or remove it if it's obsolete."),
}


def parse_playwright(report_json: str) -> list[Finding]:
    """Parse a Playwright JSON report into failing / flaky / skipped test Findings."""
    try:
        data = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []

    def walk(suite: dict, file_hint: str) -> None:
        file = suite.get("file") or file_hint or suite.get("title", "")
        for spec in suite.get("specs", []):
            sfile = spec.get("file") or file
            title = spec.get("title", "")
            for test in spec.get("tests", []):
                status = test.get("status", "")
                meta = _PW_STATUS.get(status)
                if not meta:
                    continue
                kind, sev, advice = meta
                retries = max((r.get("retry", 0) for r in test.get("results", [])), default=0)
                suffix = f" (passed on retry {retries})" if kind == "flaky-test" and retries else ""
                findings.append(Finding(
                    kind=kind, path=sfile,
                    summary=f"{kind}: {title}{suffix} [{sfile}]",
                    detail=advice, severity=sev, estimated_lines=12, symbol=title,
                ))
        for sub in suite.get("suites", []):
            walk(sub, file)

    for suite in data.get("suites", []):
        walk(suite, "")
    return findings


INSTRUCTION = """\
You are the Test Quality & Coverage specialist for Python (coverage.py) and JS/TS
(Jest/Vitest unit coverage + Playwright E2E). For coverage gaps, add focused tests that exercise
the uncovered branches/lines and assert real behavior — never modify production code to game
coverage. For Playwright results: stabilize genuinely flaky tests (web-first assertions, proper
locators), re-enable or remove skipped tests, and for a failing test, determine whether it's a
real product regression (file a bug) or a stale test — never edit a test just to make it pass.
New/changed tests must pass.
"""


def build_coverage_agent(env: "Environment", memory: Any) -> Any:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    return Agent(
        name="test_coverage",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=[add_info_to_state],
        after_agent_callback=make_persist_callback(memory),
    )
