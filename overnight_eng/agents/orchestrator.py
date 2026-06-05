"""Root Orchestrator — schedules sweeps, routes to specialists, owns memory, writes the digest.

Mirrors Dev Signal's root orchestrator: a hierarchical ADK ``Agent`` with ``sub_agents``,
``PreloadMemoryTool`` for proactive cross-night recall, and an ``after_agent_callback`` that
persists the session. For the MVP only the Signal Triage sub-agent is wired; the rest of the
fleet plugs into the same ``sub_agents`` list as it comes online.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from overnight_eng.agents.callbacks import make_persist_callback
from overnight_eng.agents.signal_triage import build_signal_triage_agent

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment
    from overnight_eng.memory.store import FleetMemory

INSTRUCTION = """\
You are the orchestrator of an overnight engineering fleet running on the developer's machine.

Your job each sweep:
1. Preload long-term memory for standing preferences and "do not refile X" decisions.
2. Route each incoming signal to the right specialist (triage for Sentry/Grafana today).
3. Collect each specialist's `*_finding` from session state and turn approved findings into
   PolicyGuard-gated actions (file issue, open DRAFT pr, comment) — never merge or push to main.
4. Record what you did/skipped (and why) for the morning digest.

Operate under propose-only autonomy. Prefer small, reviewable changes. When unsure, file an
issue rather than open a PR. Default to silence — only note decisions worth the developer's time.
"""


def build_root_agent(
    env: "Environment",
    memory: "FleetMemory",
    toolsets: dict[str, Any],
) -> Any:
    """Construct the root orchestrator with its sub-agents and memory tools."""
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.tools import LoadMemoryTool, PreloadMemoryTool

    triage = build_signal_triage_agent(env, memory, toolsets)

    return Agent(
        name="root_orchestrator",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=[PreloadMemoryTool(), LoadMemoryTool()],
        sub_agents=[triage],
        after_agent_callback=make_persist_callback(memory),
    )


def build_app(env: "Environment", memory: "FleetMemory", toolsets: dict[str, Any]) -> Any:
    """Wrap the root agent in an ADK App (the unit `adk web` / the FastAPI server serve)."""
    from google.adk.apps import App

    return App(name="overnight_eng", root_agent=build_root_agent(env, memory, toolsets))
