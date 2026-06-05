"""Signal Triage agent — the MVP vertical.

Consumes Sentry (+Grafana) via MCP, de-duplicates against long-term memory, classifies
severity, hypothesizes a root cause, and files a backlog issue through the PolicyGuard. The
deterministic heuristic in ``triage.severity`` gives the model a prior and a fallback.

ADK is imported lazily inside the factory so this module imports without the runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment
    from overnight_eng.memory.store import FleetMemory

INSTRUCTION = """\
You are the Signal Triage specialist in an overnight engineering fleet.

For each incoming signal (a Sentry issue or Grafana alert):
1. Call `load_memory`/check provided context to see if this problem was ALREADY filed. If its
   fingerprint is known, DO NOT open a new issue — leave a short comment bumping the existing
   one and stop.
2. Classify severity (critical/high/medium/low). A deterministic baseline is provided as
   `severity_hint`; only override it when the evidence clearly warrants it, and say why.
3. Investigate using the Sentry/Grafana MCP tools (recent events, frequency, related metrics)
   and write a concise root-cause HYPOTHESIS — do not assert certainty.
4. Produce a backlog issue: a crisp title, the hypothesis, links, and 1-3 suggested labels.
   Save the full draft with `add_info_to_state(key="triage_finding", data=...)` so the
   orchestrator can file it through the PolicyGuard.

Be terse and source-grounded. Never invent stack traces. Propose, do not act on, anything
destructive — you have no authority to merge, push to main, or close others' work.
"""


def build_signal_triage_agent(
    env: "Environment",
    memory: "FleetMemory",
    toolsets: dict[str, Any],
) -> Any:
    """Construct the ADK triage agent wired to Sentry/Grafana MCP + memory."""
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.tools import LoadMemoryTool

    tools: list[Any] = [LoadMemoryTool(), add_info_to_state]
    for name in ("sentry", "grafana"):
        if name in toolsets:
            tools.append(toolsets[name])

    return Agent(
        name="signal_triage",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=tools,
        after_agent_callback=make_persist_callback(memory),
    )
