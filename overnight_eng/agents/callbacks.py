"""Shared ADK callbacks/tools — the state-handoff + memory-persist primitives.

Direct analogues of Dev Signal's ``add_info_to_state`` (short-term handoff between agents via
session state, read downstream as ``{{ template_var }}``) and ``save_session_to_memory_callback``
(``after_agent_callback`` that persists salient context to long-term memory). Here long-term
memory is :class:`overnight_eng.memory.store.FleetMemory` instead of Vertex Memory Bank.
"""

from __future__ import annotations

from typing import Any


def add_info_to_state(tool_context: Any, key: str, data: str) -> dict[str, str]:
    """Save a finding to session state so a downstream agent (e.g. the Code Surgeon) can read it."""
    tool_context.state[key] = data
    return {"status": "success", "message": f"saved '{key}' to state"}


def make_persist_callback(memory: Any) -> Any:
    """Build an ``after_agent_callback`` that writes salient turn context to long-term memory."""

    async def save_session_to_memory_callback(*args: Any, **kwargs: Any) -> None:
        ctx = kwargs.get("callback_context") or (args[0] if args else None)
        if ctx is None:
            return
        # Persist anything the agent explicitly flagged for long-term recall.
        state = getattr(ctx, "state", {}) or {}
        note = state.get("memory_note")
        if note:
            memory.remember_fact(str(note))

    return save_session_to_memory_callback
