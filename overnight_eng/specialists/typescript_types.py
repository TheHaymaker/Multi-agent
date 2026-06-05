"""TypeScript Type-Hygiene specialist.

Finds permissive type signatures — explicit ``any``, implicit ``any``, and unsafe casts —
from ``tsc --noEmit`` and ESLint (``@typescript-eslint`` rules), then turns them into
**small, atomic** change batches (clustered by file, capped per PR) so each draft PR replaces
a tight set of loose types with precise ones. Low blast radius, easy review.

Parsers are pure (text/JSON in, Findings out) so detection is unit-tested without a TS repo.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from overnight_eng.models import Severity
from overnight_eng.specialists.base import Finding

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

# Default checks the Code Surgeon runs before any draft PR is opened.
DEFAULT_CHECKS = ["npx tsc --noEmit", "npm test --silent"]

# Rules we treat as type-hygiene targets (most → least severe).
_RULE_SEVERITY = {
    "@typescript-eslint/no-explicit-any": Severity.MEDIUM,
    "@typescript-eslint/no-unsafe-assignment": Severity.HIGH,
    "@typescript-eslint/no-unsafe-member-access": Severity.HIGH,
    "@typescript-eslint/no-unsafe-call": Severity.HIGH,
    "@typescript-eslint/no-unsafe-return": Severity.HIGH,
    "@typescript-eslint/no-unsafe-argument": Severity.HIGH,
}

# tsc implicit-any diagnostics worth fixing (TS7006 param, TS7005 var, TS7031 binding, etc.)
_TSC_IMPLICIT_ANY = {"TS7005", "TS7006", "TS7008", "TS7031", "TS7034", "TS7053"}
_TSC_LINE = re.compile(r"^(?P<path>[^()]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$")


def parse_eslint(report_json: str) -> list[Finding]:
    """Parse ESLint JSON output into type-hygiene Findings (ignores unrelated rules)."""
    try:
        files = json.loads(report_json)
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for entry in files:
        path = entry.get("filePath", "")
        for msg in entry.get("messages", []):
            rule = msg.get("ruleId")
            if rule not in _RULE_SEVERITY:
                continue
            findings.append(
                Finding(
                    kind="explicit-any" if "no-explicit-any" in rule else "unsafe-type",
                    path=path,
                    summary=f"{rule} at {path}:{msg.get('line', '?')}",
                    detail=msg.get("message", ""),
                    severity=_RULE_SEVERITY[rule],
                    estimated_lines=6,  # replacing a signature is usually a few lines
                    symbol=_enclosing_symbol(msg.get("message", "")),
                )
            )
    return findings


def parse_tsc(output: str) -> list[Finding]:
    """Parse ``tsc --noEmit`` stderr/stdout into implicit-any Findings."""
    findings: list[Finding] = []
    for raw in output.splitlines():
        m = _TSC_LINE.match(raw.strip())
        if not m or m.group("code") not in _TSC_IMPLICIT_ANY:
            continue
        findings.append(
            Finding(
                kind="implicit-any",
                path=m.group("path").strip(),
                summary=f"{m.group('code')} at {m.group('path').strip()}:{m.group('line')}",
                detail=m.group("msg"),
                severity=Severity.MEDIUM,
                estimated_lines=5,
            )
        )
    return findings


def _enclosing_symbol(message: str) -> str:
    # ESLint messages sometimes name the symbol, e.g. "Unsafe assignment ... to 'user'".
    m = re.search(r"'([A-Za-z_$][\w$]*)'", message)
    return m.group(1) if m else ""


INSTRUCTION = """\
You are the TypeScript Type-Hygiene specialist. Replace permissive types (explicit/implicit
`any`, unsafe casts) with the most precise correct types. Rules:
- Touch ONLY the files/symbols in the batch. Do NOT refactor logic or change runtime behavior.
- Prefer narrowing to real types/interfaces over `unknown`; never silence with `// eslint-disable`.
- Keep each PR tiny (under the line cap). If a fix would cascade widely, propose the smallest
  safe slice and note the follow-ups instead of expanding the diff.
- The change MUST pass `tsc --noEmit` and the test suite before you finish.
"""


def build_typescript_agent(env: "Environment", memory: Any) -> Any:
    """ADK agent for nuanced type fixes (delegates edits to the Code Surgeon)."""
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    return Agent(
        name="typescript_types",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=[add_info_to_state],
        after_agent_callback=make_persist_callback(memory),
    )
