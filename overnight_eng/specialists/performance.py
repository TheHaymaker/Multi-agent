"""Performance specialist — detects regressions from pytest-benchmark output vs a baseline.

Compares each benchmark's mean against a stored baseline; a slowdown beyond ``threshold`` is a
regression Finding (it files an issue rather than auto-editing, since perf fixes usually need
human judgment — but it can hand a hot path to the Code Surgeon when the fix is obvious).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["pytest --benchmark-only --benchmark-json=bench.json"]


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


INSTRUCTION = """\
You are the Performance specialist. For each regression, identify the likely hot path from the
benchmark name and recent diffs, propose the smallest optimization that restores the baseline,
and re-run the benchmark to confirm. If the cause is unclear or the fix is risky, file an issue
with your analysis instead of editing.
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
