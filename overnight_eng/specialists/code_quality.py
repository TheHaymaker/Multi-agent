"""Code Quality specialist — turns linter output (ruff/semgrep) into small fix batches."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["ruff check .", "pytest -q"]

# ruff rule-prefix -> severity (auto-fixable style issues are low; bug-class lint is higher).
_PREFIX_SEVERITY = {
    "F": Severity.HIGH,     # pyflakes (undefined names, unused — real bugs)
    "B": Severity.HIGH,     # flake8-bugbear
    "S": Severity.HIGH,     # bandit security
    "E": Severity.LOW,      # pycodestyle
    "W": Severity.LOW,
    "I": Severity.LOW,      # import sorting
}


def _severity_for(code: str) -> Severity:
    return _PREFIX_SEVERITY.get(code[:1], Severity.MEDIUM)


def parse_ruff(report_json: str) -> list[Finding]:
    """Parse ``ruff check --output-format=json`` into Findings."""
    try:
        items = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for it in items:
        code = it.get("code") or ""
        path = it.get("filename", "")
        row = (it.get("location") or {}).get("row", "?")
        fixable = bool(it.get("fix"))
        findings.append(
            Finding(
                kind="lint",
                path=path,
                summary=f"{code} at {path}:{row}",
                detail=it.get("message", "") + (" (autofixable)" if fixable else ""),
                severity=_severity_for(code),
                estimated_lines=2 if fixable else 6,
            )
        )
    return findings


INSTRUCTION = """\
You are the Code Quality specialist. Resolve the linter findings in the batch with the
smallest correct change; prefer the autofix where it's safe, but verify behavior is preserved.
Do not suppress with noqa. The change must pass `ruff check` and the tests.
"""


def build_code_quality_agent(env: "Environment", memory: Any) -> Any:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    return Agent(
        name="code_quality",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=[add_info_to_state],
        after_agent_callback=make_persist_callback(memory),
    )
