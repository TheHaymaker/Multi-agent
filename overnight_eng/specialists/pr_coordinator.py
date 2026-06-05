"""PR Coordinator specialist — keeps the fleet's own PRs healthy and responsive.

Given the open PRs (from the GitHub/GitLab MCP server), it plans propose-only actions:
  * **Rebase** an agent-owned branch that has fallen behind its base (only when conflict-free).
  * **Respond** to unresolved review comments — acknowledge + plan edits (handed to the Code
    Surgeon as a small draft-PR update).
  * **Advise** on non-agent PRs via a comment only — it never rebases or edits someone else's
    branch.

The planner is pure (PR state in, ActionRequests out) so routing/guardrails are unit-tested
without a live forge. ``coordinate_prs`` gates the planned actions through the PolicyGuard.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from overnight_eng.models import ActionRequest, ActionType

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment
    from overnight_eng.tools.policy_guard import PolicyGuard

DEFAULT_AGENT_GLOBS = ("agent/*", "overnight/*", "bot/*")


@dataclass
class PullRequest:
    """Normalized PR state (subset the coordinator reasons over)."""

    number: int
    branch: str                 # head branch
    base: str                   # base branch (e.g. "main")
    repo: str = ""
    behind_by: int = 0          # commits the head is behind base
    is_draft: bool = True
    has_conflicts: bool = False
    unresolved_comments: list[str] = field(default_factory=list)


def _is_agent_owned(branch: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(branch, g) for g in globs)


def plan_pr_actions(
    pr: PullRequest,
    *,
    agent_globs: tuple[str, ...] = DEFAULT_AGENT_GLOBS,
) -> list[ActionRequest]:
    """Decide the propose-only actions for a single PR."""
    actions: list[ActionRequest] = []
    owned = _is_agent_owned(pr.branch, agent_globs)

    if not owned:
        # Advisory only — never write to someone else's branch.
        if pr.unresolved_comments:
            actions.append(ActionRequest(
                ActionType.COMMENT, actor="pr_coordinator",
                summary=f"advisory on #{pr.number}: {len(pr.unresolved_comments)} open thread(s)",
                repo=pr.repo, payload={"pr": pr.number},
            ))
        return actions

    # --- agent-owned PRs ---
    if pr.behind_by > 0:
        if pr.has_conflicts:
            # Don't auto-rebase into a conflict; surface it for the human.
            actions.append(ActionRequest(
                ActionType.COMMENT, actor="pr_coordinator",
                summary=f"#{pr.number} is {pr.behind_by} behind {pr.base} and conflicts — needs a human",
                branch=pr.branch, repo=pr.repo, payload={"pr": pr.number},
            ))
        else:
            actions.append(ActionRequest(
                ActionType.REBASE_AGENT_BRANCH, actor="pr_coordinator",
                summary=f"rebase #{pr.number} ({pr.branch}) onto {pr.base} ({pr.behind_by} behind)",
                branch=pr.branch, target_branch=pr.base, repo=pr.repo, payload={"pr": pr.number},
            ))

    if pr.unresolved_comments:
        actions.append(ActionRequest(
            ActionType.COMMENT, actor="pr_coordinator",
            summary=f"acknowledge {len(pr.unresolved_comments)} review thread(s) on #{pr.number}",
            branch=pr.branch, repo=pr.repo, payload={"pr": pr.number, "threads": pr.unresolved_comments},
        ))
        # Plan the edits as a small draft-PR update (Code Surgeon makes the actual changes).
        actions.append(ActionRequest(
            ActionType.UPDATE_DRAFT_PR, actor="pr_coordinator",
            summary=f"address review feedback on #{pr.number}",
            branch=pr.branch, target_branch=pr.base, repo=pr.repo,
            payload={"pr": pr.number, "threads": pr.unresolved_comments},
        ))

    return actions


def coordinate_prs(
    guard: "PolicyGuard",
    prs: list[PullRequest],
    *,
    execute: Any | None = None,
    agent_globs: tuple[str, ...] = DEFAULT_AGENT_GLOBS,
) -> None:
    """Plan + gate actions for every PR. ``execute`` performs the MCP/git call (injected)."""
    for pr in prs:
        for req in plan_pr_actions(pr, agent_globs=agent_globs):
            guard.guard(req, execute or _default_execute)


def _default_execute(req: ActionRequest) -> str:
    return f"{req.action.value}: {req.summary}"


INSTRUCTION = """\
You are the PR Coordinator. For each of the fleet's own open PRs:
- If it's behind its base and conflict-free, rebase it; if it conflicts, comment and stop.
- Read unresolved review threads, reply concisely, and plan the smallest edits that address
  them — hand those edits to the Code Surgeon as a draft-PR update.
- Never merge, never push to a protected branch, never touch a PR that isn't agent-owned
  beyond an advisory comment.
"""


def build_pr_coordinator_agent(env: "Environment", memory: Any, toolsets: dict[str, Any]) -> Any:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    tools: list[Any] = [add_info_to_state]
    for name in ("github", "gitlab"):
        if name in toolsets:
            tools.append(toolsets[name])

    return Agent(
        name="pr_coordinator",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=tools,
        after_agent_callback=make_persist_callback(memory),
    )
