"""Backlog / Jira specialist — reconcile the fleet's filed issues with the Jira backlog.

It polls Jira (Atlassian MCP) and cross-references against what the fleet has filed (GitHub/
GitLab issues stamped with a fingerprint). Goals:

  * **De-dup across systems** — don't open a Jira ticket for a problem already tracked there.
  * **Track gaps** — for a filed issue with no Jira counterpart, propose a tracking ticket with
    the right priority and a back-link.
  * **Propose priority** — when a tracked ticket's priority doesn't match the signal's severity,
    suggest a change (propose-only: a comment, never a silent re-prioritize).

The reconciler is pure (filed items + Jira issues -> ActionRequests) so it's unit-tested
without a live Jira. Fingerprints are matched first; a normalized-title fallback catches
tickets filed by humans before the fleet existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from overnight_eng.models import ActionRequest, ActionType, Severity
from overnight_eng.triage.dedupe import normalize_title

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment

# Convention: the fleet stamps a "fp:<fingerprint>" label on tickets it creates.
_FP_LABEL = re.compile(r"^fp:(?P<fp>[0-9a-zA-Z]+)$")

SEVERITY_TO_PRIORITY: dict[Severity, str] = {
    Severity.CRITICAL: "Highest",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.INFO: "Lowest",
}


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str = "Open"
    priority: str = ""
    labels: list[str] = field(default_factory=list)
    fingerprint: str = ""  # explicit if known; else derived from a fp: label


@dataclass
class FiledItem:
    """Something the fleet already filed in the forge that the backlog should track."""

    fingerprint: str
    title: str
    severity: Severity
    ref: str  # e.g. the GitHub issue URL


def fingerprint_of(issue: JiraIssue) -> str:
    if issue.fingerprint:
        return issue.fingerprint
    for label in issue.labels:
        m = _FP_LABEL.match(label)
        if m:
            return m.group("fp")
    return ""


def severity_to_priority(sev: Severity) -> str:
    return SEVERITY_TO_PRIORITY.get(sev, "Medium")


def _is_open(status: str) -> bool:
    return status.strip().lower() not in {"done", "closed", "resolved", "cancelled"}


def find_match(item: FiledItem, issues: list[JiraIssue]) -> JiraIssue | None:
    """Match by fingerprint first, then by normalized title (open tickets only)."""
    for issue in issues:
        if item.fingerprint and fingerprint_of(issue) == item.fingerprint:
            return issue
    norm = normalize_title(item.title)
    for issue in issues:
        if _is_open(issue.status) and normalize_title(issue.summary) == norm:
            return issue
    return None


def reconcile(
    filed: list[FiledItem],
    jira_issues: list[JiraIssue],
    *,
    create_missing: bool = True,
    project_key: str = "",
) -> list[ActionRequest]:
    """Produce propose-only actions to keep the Jira backlog in sync with filed issues."""
    actions: list[ActionRequest] = []
    for item in filed:
        match = find_match(item, jira_issues)
        desired = severity_to_priority(item.severity)

        if match is None:
            if create_missing:
                actions.append(ActionRequest(
                    ActionType.CREATE_ISSUE, actor="jira_backlog",
                    summary=f"track: {item.title}",
                    repo=project_key,
                    payload={
                        "fingerprint": item.fingerprint,
                        "labels": [f"fp:{item.fingerprint}", "overnight"],
                        "priority": desired,
                        "body": f"Tracking issue auto-created from {item.ref}.\nSeverity: "
                                f"{item.severity.value} -> priority {desired}.",
                    },
                ))
            continue

        # Tracked already — only nudge priority if it's open and mismatched.
        if _is_open(match.status) and match.priority and match.priority != desired:
            actions.append(ActionRequest(
                ActionType.COMMENT, actor="jira_backlog",
                summary=f"suggest priority {desired} for {match.key} "
                        f"(severity {item.severity.value}; currently {match.priority})",
                repo=project_key,
                payload={"key": match.key, "suggested_priority": desired},
            ))
    return actions


def reconcile_backlog(
    guard: Any,
    filed: list[FiledItem],
    jira_issues: list[JiraIssue],
    *,
    execute: Any | None = None,
    project_key: str = "",
) -> None:
    """Gate every reconciliation action through the PolicyGuard."""
    for req in reconcile(filed, jira_issues, project_key=project_key):
        guard.guard(req, execute or (lambda r: f"{r.action.value}: {r.summary}"))


def filed_items_from_memory(memory: Any) -> list[FiledItem]:
    """Build FiledItems from what the fleet recorded in long-term memory (best-effort)."""
    items: list[FiledItem] = []
    for fp, ref in getattr(memory.seen, "_seen", {}).items():
        items.append(FiledItem(fingerprint=fp, title="", severity=Severity.MEDIUM, ref=ref))
    return items


INSTRUCTION = """\
You are the Backlog/Jira specialist. Poll Jira for open issues and reconcile them with what the
fleet has filed in GitHub/GitLab:
- If a filed problem has no Jira ticket, create a tracking ticket with a fp: label, the right
  priority (from severity), and a back-link. Never create a duplicate.
- If a ticket exists but its priority doesn't match the signal's severity, comment suggesting a
  change — do not silently re-prioritize, reassign, or close anyone's ticket.
Be conservative: when unsure whether two issues are the same, comment rather than create.
"""


def build_jira_agent(env: "Environment", memory: Any, toolsets: dict[str, Any]) -> Any:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    from overnight_eng.agents.callbacks import add_info_to_state, make_persist_callback

    tools: list[Any] = [add_info_to_state]
    if "jira" in toolsets:
        tools.append(toolsets["jira"])

    return Agent(
        name="jira_backlog",
        model=LiteLlm(model=env.settings.reasoning_model),
        instruction=INSTRUCTION,
        tools=tools,
        after_agent_callback=make_persist_callback(memory),
    )
