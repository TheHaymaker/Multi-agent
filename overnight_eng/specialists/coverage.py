"""Test Quality & Coverage specialist — finds under-tested files and proposes tests.

Parses ``coverage.py`` JSON (``coverage json``) and flags files below a coverage threshold,
sized by how many lines are missing. Each gap becomes a small "add tests for X" batch. (A
mutation-testing pass via ``mutmut`` plugs in the same way: surviving mutants -> Findings.)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["pytest -q", "coverage run -m pytest && coverage json -o coverage.json"]


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
        sev = Severity.HIGH if pct < threshold - 20 else Severity.MEDIUM
        findings.append(
            Finding(
                kind="coverage-gap",
                path=path,
                summary=f"{path} at {pct:.0f}% ({missing} lines uncovered)",
                detail="Add focused unit tests for the uncovered branches/lines: "
                       + ", ".join(map(str, (info.get("missing_lines") or [])[:15])),
                severity=sev,
                # test-writing scales with the gap, but stays capped per PR by the planner
                estimated_lines=min(missing * 2, 60),
            )
        )
    return findings


INSTRUCTION = """\
You are the Test Quality & Coverage specialist. For each flagged file, add focused tests that
exercise the uncovered branches/lines — assert real behavior, not just line execution. Do not
modify production code to game coverage. New tests must pass.
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
