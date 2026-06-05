"""Morning digest — the deliverable you read with coffee.

Aggregates the night's PolicyGuard audit log into a scannable Markdown summary: what was
filed, drafted, rebased, answered, and crucially **what was skipped and why**. Pure function
of the audit records, so it's testable and can be rendered to stdout, a file, or Slack.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from overnight_eng.models import ActionRecord, ActionType

_HUMAN = {
    ActionType.CREATE_ISSUE: "Issues filed",
    ActionType.COMMENT: "Comments posted",
    ActionType.OPEN_DRAFT_PR: "Draft PRs opened",
    ActionType.UPDATE_DRAFT_PR: "Draft PRs updated",
    ActionType.REBASE_AGENT_BRANCH: "Branches rebased onto base",
    ActionType.PUSH_AGENT_BRANCH: "Agent branches pushed",
    ActionType.CREATE_BRANCH: "Branches created",
}


def render_digest(records: list[ActionRecord], *, when: datetime | None = None) -> str:
    """Render the audit log as a Markdown morning digest."""
    when = when or datetime.now(timezone.utc)
    executed = [r for r in records if r.executed]
    denied = [r for r in records if not r.allowed]
    dry = [r for r in records if r.dry_run and r.allowed]

    lines: list[str] = []
    lines.append(f"# 🌅 Overnight Engineering — {when:%Y-%m-%d %H:%M UTC}")
    lines.append("")
    lines.append(
        f"**{len(executed)} actions taken**, {len(dry)} proposed (dry-run), "
        f"{len(denied)} blocked by policy."
    )
    lines.append("")

    # What was done, grouped by action type.
    done = Counter(r.request.action for r in executed)
    if done:
        lines.append("## What I did")
        for action, count in done.most_common():
            label = _HUMAN.get(action, action.value)
            lines.append(f"- **{label}:** {count}")
            for r in executed:
                if r.request.action is action:
                    ref = (r.result or r.request.summary)[:120]
                    lines.append(f"  - {r.request.summary} — {ref}")
        lines.append("")

    # Proposed-only (dry run) — what it *would* do.
    if dry:
        lines.append("## Proposed (dry-run, not executed)")
        for r in dry:
            lines.append(f"- {r.request.action.value}: {r.request.summary}")
        lines.append("")

    # Blocked — the guardrails earning their keep.
    if denied:
        lines.append("## Skipped by policy (for your awareness)")
        for r in denied:
            lines.append(f"- `{r.request.action.value}` — {r.reason} ({r.request.summary})")
        lines.append("")

    if not (executed or dry or denied):
        lines.append("_Quiet night — nothing actionable._")

    return "\n".join(lines).rstrip() + "\n"
