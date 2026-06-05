"""Performance specialist — detects regressions across Python and JS/TS/Next.js front-ends.

Sources:
  * Python: ``pytest-benchmark`` JSON vs a stored baseline (:func:`detect_regressions`).
  * Next.js: ``next build`` route sizes — First Load JS per route, vs a baseline and/or an
    absolute budget (:func:`parse_next_build`).
  * Any web app: Lighthouse JSON — performance score + Core Web Vitals (LCP/TBT/CLS) vs
    budgets (:func:`parse_lighthouse`).

All detectors are pure (report in, Findings out). Performance findings are filed as **issues**
(not auto-edited): the fix usually needs human judgment, so the agent reports with analysis.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["pytest --benchmark-only --benchmark-json=bench.json"]
# JS/Next.js perf gathering (the agent files issues; these are how a human reproduces).
NEXT_CHECKS = ["next build"]
LIGHTHOUSE_CHECKS = ["npx --yes @lhci/cli autorun"]

# Default Core Web Vitals budgets (Lighthouse "good" thresholds).
DEFAULT_LH_BUDGETS = {"performance": 0.90, "lcp_ms": 2500.0, "tbt_ms": 300.0, "cls": 0.10}


def detect_regressions(
    bench_json: str,
    baseline: dict[str, float],
    *,
    threshold: float = 0.20,
) -> list[Finding]:
    """Return Findings for benchmarks whose mean exceeds ``baseline * (1 + threshold)``."""
    try:
        data = json.loads(bench_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for b in data.get("benchmarks", []):
        name = b.get("fullname") or b.get("name", "")
        mean = float((b.get("stats") or {}).get("mean", 0.0))
        base = baseline.get(name)
        if not base or mean <= 0:
            continue
        ratio = mean / base
        if ratio > 1 + threshold:
            slow_pct = (ratio - 1) * 100
            sev = Severity.HIGH if ratio > 1.5 else Severity.MEDIUM
            findings.append(
                Finding(
                    kind="perf-regression",
                    path=_path_of(name),
                    summary=f"{name} {slow_pct:.0f}% slower (mean {mean:.4g}s vs {base:.4g}s)",
                    detail="Profile the hot path (py-spy/scalene) before optimizing; verify the "
                           "benchmark recovers and tests still pass.",
                    severity=sev,
                    estimated_lines=20,
                    symbol=name,
                )
            )
    return findings


def _path_of(fullname: str) -> str:
    # pytest fullname like "tests/test_x.py::test_bench" -> file path
    return fullname.split("::", 1)[0] if "::" in fullname else fullname


# --- Next.js bundle size ----------------------------------------------------------

_SIZE_UNITS = {"b": 1 / 1024, "kb": 1.0, "mb": 1024.0}
_TABLE_ROW = re.compile(r"[┌├└│]?\s*[○●ƒλ○●ƒ]?\s*(?P<route>/\S*)\s+.*?(?P<first>[\d.]+)\s*(?P<unit>kB|MB|B)\s*$")


def _to_kb(value: float, unit: str) -> float:
    return value * _SIZE_UNITS.get(unit.lower(), 1.0)


def _routes_from_report(data: str) -> dict[str, float]:
    """Accept either JSON ({"routes": {route: kB}}) or raw `next build` table text."""
    try:
        obj = json.loads(data)
        routes = obj.get("routes", obj) if isinstance(obj, dict) else {}
        return {str(k): float(v) for k, v in routes.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    out: dict[str, float] = {}
    for line in data.splitlines():
        m = _TABLE_ROW.search(line)
        if m:
            out[m.group("route")] = _to_kb(float(m.group("first")), m.group("unit"))
    return out


def parse_next_build(
    data: str,
    baseline: dict[str, float] | None = None,
    *,
    threshold: float = 0.15,
    budget_kb: float | None = None,
) -> list[Finding]:
    """Flag routes whose First Load JS regressed past ``threshold`` or exceeds ``budget_kb``."""
    baseline = baseline or {}
    findings: list[Finding] = []
    for route, kb in _routes_from_report(data).items():
        base = baseline.get(route)
        if base and kb > base * (1 + threshold):
            grew = (kb / base - 1) * 100
            findings.append(Finding(
                kind="bundle-regression", path=f"app{route}" if route != "/" else "app/page",
                summary=f"{route} First Load JS {grew:.0f}% larger ({kb:.0f} kB vs {base:.0f} kB)",
                detail="A route's client bundle grew. Check for new heavy imports, missing "
                       "dynamic() / code-splitting, or a server component pulled client-side.",
                severity=Severity.HIGH if kb > base * 1.4 else Severity.MEDIUM,
                estimated_lines=15, symbol=route,
            ))
        elif budget_kb and kb > budget_kb:
            findings.append(Finding(
                kind="bundle-budget", path=f"app{route}" if route != "/" else "app/page",
                summary=f"{route} First Load JS {kb:.0f} kB over budget ({budget_kb:.0f} kB)",
                detail="Route exceeds the bundle budget; split or lazy-load heavy dependencies.",
                severity=Severity.MEDIUM, estimated_lines=15, symbol=route,
            ))
    return findings


# --- Lighthouse / Core Web Vitals -------------------------------------------------

def parse_lighthouse(lhr_json: str, budgets: dict[str, float] | None = None) -> list[Finding]:
    """Flag a Lighthouse run that misses the performance score or Core Web Vitals budgets."""
    budgets = {**DEFAULT_LH_BUDGETS, **(budgets or {})}
    try:
        lhr = json.loads(lhr_json)
    except json.JSONDecodeError:
        return []
    url = lhr.get("finalUrl") or lhr.get("requestedUrl") or "page"
    audits = lhr.get("audits", {})
    findings: list[Finding] = []

    score = (lhr.get("categories", {}).get("performance", {}) or {}).get("score")
    if score is not None and score < budgets["performance"]:
        findings.append(Finding(
            kind="web-vitals", path=url,
            summary=f"Lighthouse performance {score * 100:.0f} < {budgets['performance'] * 100:.0f} ({url})",
            detail="Overall performance score regressed; see the LCP/TBT/CLS findings for drivers.",
            severity=Severity.HIGH if score < budgets["performance"] - 0.1 else Severity.MEDIUM,
            estimated_lines=20, symbol="performance",
        ))

    for metric, audit_id, unit, fmt in (
        ("lcp_ms", "largest-contentful-paint", "ms", "{:.0f} ms"),
        ("tbt_ms", "total-blocking-time", "ms", "{:.0f} ms"),
        ("cls", "cumulative-layout-shift", "", "{:.2f}"),
    ):
        val = (audits.get(audit_id, {}) or {}).get("numericValue")
        budget = budgets[metric]
        if val is not None and val > budget:
            over = (val / budget - 1) * 100
            findings.append(Finding(
                kind="web-vitals", path=url,
                summary=f"{audit_id} {fmt.format(val)} > budget {fmt.format(budget)} ({over:.0f}% over) @ {url}",
                detail="Core Web Vital over budget; profile with the Lighthouse trace before fixing.",
                severity=Severity.HIGH if val > budget * 1.5 else Severity.MEDIUM,
                estimated_lines=20, symbol=audit_id,
            ))
    return findings


INSTRUCTION = """\
You are the Performance specialist for Python and JS/TS/Next.js/React apps. For each regression
(pytest-benchmark slowdown, Next.js route bundle growth, or Lighthouse/Core-Web-Vitals miss):
identify the likely cause from the metric and recent diffs, and FILE AN ISSUE with your analysis
and a concrete suggested fix (e.g. dynamic import, memoization, image optimization, code-split).
Only hand a fix to the Code Surgeon when it's small and obviously safe.
"""


def build_performance_agent(env: "Environment", memory: Any) -> Any:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    return Agent(
        name="performance",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=[add_info_to_state],
        after_agent_callback=make_persist_callback(memory),
    )
