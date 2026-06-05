"""Code Quality specialist — Python (ruff) and JS/TS/React/Next.js (ESLint) into fix batches.

Two parsers, one specialist:
  * :func:`parse_ruff` — Python lint (pyflakes/bugbear/security as bugs, style as low).
  * :func:`parse_eslint` — React/Next quality rules (hooks, jsx-key, ``@next/next/*``, a11y,
    import cycles). It deliberately skips the *type-hygiene* rules (``no-explicit-any`` /
    ``no-unsafe-*``) — those belong to the TypeScript Type-Hygiene specialist.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding
from overnight_eng.specialists.typescript_types import TYPE_HYGIENE_RULES

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

DEFAULT_CHECKS = ["ruff check .", "pytest -q"]
# Checks the Code Surgeon runs for JS/TS/React/Next fixes before opening a draft PR.
JS_CHECKS = ["npx --yes eslint .", "npm test --silent"]

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


# Known React/Next/import quality rules -> severity (most are bug-class, not style).
_ESLINT_RULE_SEVERITY = {
    "react-hooks/rules-of-hooks": Severity.HIGH,      # breaks the rules of hooks = real bug
    "react-hooks/exhaustive-deps": Severity.HIGH,     # stale closures / missing deps
    "react/jsx-key": Severity.HIGH,                    # missing list keys
    "react/no-direct-mutation-state": Severity.HIGH,
    "import/no-cycle": Severity.HIGH,
    "@next/next/no-sync-scripts": Severity.HIGH,
    "@next/next/no-html-link-for-pages": Severity.MEDIUM,
    "@next/next/no-img-element": Severity.LOW,
    "react/no-unescaped-entities": Severity.LOW,
    "no-console": Severity.LOW,
    "eqeqeq": Severity.MEDIUM,
}
# Family prefixes for rules not enumerated above.
_ESLINT_FAMILY = (
    ("react-hooks/", Severity.HIGH),
    ("jsx-a11y/", Severity.MEDIUM),
    ("@next/next/", Severity.MEDIUM),
    ("react/", Severity.MEDIUM),
    ("import/", Severity.LOW),
)
# ESLint severity 2 = error, 1 = warning.
_ESLINT_ERROR = 2


def _eslint_severity(rule: str, level: int) -> Severity | None:
    if rule in _ESLINT_RULE_SEVERITY:
        return _ESLINT_RULE_SEVERITY[rule]
    for prefix, sev in _ESLINT_FAMILY:
        if rule.startswith(prefix):
            return sev
    # Unknown rule: take it only if ESLint flagged it as an error (avoid style-warning noise).
    return Severity.MEDIUM if level >= _ESLINT_ERROR else None


def parse_eslint(report_json: str) -> list[Finding]:
    """Parse ESLint JSON into React/Next quality Findings (skips type-hygiene rules)."""
    try:
        files = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for entry in files:
        path = entry.get("filePath", "")
        for msg in entry.get("messages", []):
            rule = msg.get("ruleId")
            if not rule or rule in TYPE_HYGIENE_RULES:  # types owned by typescript_types
                continue
            sev = _eslint_severity(rule, int(msg.get("severity", 1)))
            if sev is None:
                continue
            findings.append(Finding(
                kind="lint",
                path=path,
                summary=f"{rule} at {path}:{msg.get('line', '?')}",
                detail=msg.get("message", ""),
                severity=sev,
                estimated_lines=4,
                symbol=_symbol_of(msg.get("message", "")),
            ))
    return findings


def _symbol_of(message: str) -> str:
    m = re.search(r"'([A-Za-z_$][\w$]*)'", message)
    return m.group(1) if m else ""


INSTRUCTION = """\
You are the Code Quality specialist for Python (ruff) and JS/TS/React/Next.js (ESLint). Resolve
the findings in the batch with the smallest correct change; prefer a safe autofix but verify
behavior is preserved. For React hooks issues, fix the dependency array or hook placement rather
than disabling the rule. Never suppress with noqa / eslint-disable. The change must pass the
relevant linter (`ruff check` or `eslint`) and the test suite.
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
