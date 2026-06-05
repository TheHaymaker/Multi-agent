"""Shared specialist primitives: Findings, small-PR batching, and the code-work driver.

The batching planner is the mechanism behind "small PRs, low blast radius": findings are
grouped (by file, or by symbol cluster) and greedily packed so each batch stays under a line
cap and a per-PR finding cap. One batch becomes one draft PR via the Code Surgeon, gated by
the PolicyGuard. All of this is pure/injectable so it's unit-testable without a live forge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from overnight_eng.models import ActionRequest, ActionType, Severity
from overnight_eng.workers.code_surgeon import ChangeProposal, TaskSpec

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.tools.policy_guard import PolicyGuard
    from overnight_eng.workers.code_surgeon import CodeSurgeon


@dataclass
class Finding:
    """A normalized, source-agnostic improvement opportunity."""

    kind: str               # e.g. "explicit-any", "lint", "coverage-gap", "perf-regression"
    path: str               # file the change touches
    summary: str            # one-line description
    detail: str = ""        # extra context / suggested fix for the Code Surgeon
    severity: Severity = Severity.MEDIUM
    estimated_lines: int = 10   # rough change size, used to respect PR line caps
    symbol: str = ""        # optional function/class for tighter clustering


@dataclass
class ChangeBatch:
    """A small, atomic unit of work -> one draft PR."""

    title: str
    findings: list[Finding]
    kind: str = ""

    @property
    def estimated_lines(self) -> int:
        return sum(f.estimated_lines for f in self.findings)

    @property
    def paths(self) -> list[str]:
        seen: dict[str, None] = {}
        for f in self.findings:
            seen.setdefault(f.path, None)
        return list(seen)


def plan_batches(
    findings: list[Finding],
    *,
    max_lines: int = 150,
    max_findings: int = 8,
    group_by: str = "path",
    title_prefix: str = "chore",
) -> list[ChangeBatch]:
    """Pack findings into small batches (one draft PR each).

    Groups by ``path`` (default) or ``symbol`` so a batch is cohesive and reviewable, then
    greedily splits a group when it would exceed ``max_lines`` or ``max_findings``. A single
    oversized finding still gets its own batch (the Code Surgeon enforces the hard cap and may
    reject/re-scope it) rather than being silently dropped.
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        key = (f.symbol or f.path) if group_by == "symbol" else f.path
        groups.setdefault(key, []).append(f)

    batches: list[ChangeBatch] = []
    for key, items in groups.items():
        # severity desc, then larger first, so the most important change leads each PR
        items = sorted(items, key=lambda f: (_sev_rank(f.severity), f.estimated_lines), reverse=True)
        current: list[Finding] = []
        lines = 0
        for f in items:
            if current and (lines + f.estimated_lines > max_lines or len(current) >= max_findings):
                batches.append(_make_batch(title_prefix, key, current))
                current, lines = [], 0
            current.append(f)
            lines += f.estimated_lines
        if current:
            batches.append(_make_batch(title_prefix, key, current))
    return batches


def _sev_rank(sev: Severity) -> int:
    order = {Severity.CRITICAL: 4, Severity.HIGH: 3, Severity.MEDIUM: 2, Severity.LOW: 1, Severity.INFO: 0}
    return order.get(sev, 0)


def _make_batch(prefix: str, key: str, items: list[Finding]) -> ChangeBatch:
    kind = items[0].kind
    short = key.rsplit("/", 1)[-1]
    title = f"{prefix}({kind}): {short} ({len(items)} item{'s' if len(items) != 1 else ''})"
    return ChangeBatch(title=title, findings=list(items), kind=kind)


def batch_to_taskspec(
    batch: ChangeBatch,
    *,
    repo_path: str,
    base_branch: str,
    run_checks: list[str],
    max_lines: int = 150,
    branch_prefix: str = "agent",
) -> TaskSpec:
    """Compose a Code Surgeon task from a batch's findings."""
    lines = "\n".join(f"- {f.path}: {f.summary}" + (f"\n    {f.detail}" if f.detail else "")
                      for f in batch.findings)
    instructions = (
        f"Make the smallest correct change addressing ONLY these findings "
        f"(kind: {batch.kind}). Do not refactor unrelated code.\n\n{lines}"
    )
    return TaskSpec(
        repo_path=repo_path,
        base_branch=base_branch,
        title=batch.title,
        instructions=instructions,
        run_checks=run_checks,
        max_changed_lines=max_lines,
        branch_prefix=branch_prefix,
    )


async def run_code_work(
    *,
    guard: "PolicyGuard",
    surgeon: "CodeSurgeon",
    batches: list[ChangeBatch],
    repo_path: str,
    base_branch: str,
    run_checks: list[str],
    actor: str,
    max_lines: int = 150,
    branch_prefix: str = "agent",
    open_pr: Callable[[ChangeProposal], str] | None = None,
) -> list[ChangeProposal]:
    """Run each batch through the surgeon; gate the resulting draft PR through the guard.

    ``open_pr`` is the executor that actually creates the draft PR (GitHub/GitLab MCP). It's
    injected so this driver is testable with a fake. Oversized or check-failing proposals are
    recorded (denied/skipped) and never opened.
    """
    proposals: list[ChangeProposal] = []
    for batch in batches:
        # All fleet branches must be agent-owned (PolicyGuard only writes to agent/* etc.).
        spec = batch_to_taskspec(
            batch, repo_path=repo_path, base_branch=base_branch,
            run_checks=run_checks, max_lines=max_lines, branch_prefix=branch_prefix,
        )
        proposal = await surgeon.run(spec)
        proposals.append(proposal)

        # Defense-in-depth: enforce the small-PR cap at the gate, not just in the surgeon.
        if proposal.changed_lines > max_lines and not proposal.rejected_reason:
            proposal.rejected_reason = (
                f"diff too large ({proposal.changed_lines} > {max_lines} lines)"
            )

        if not proposal.ok:
            # Record the skip so it shows up in the digest with a reason.
            guard.audit_log.append(_skipped_record(actor, batch, proposal))
            continue

        req = ActionRequest(
            action=ActionType.OPEN_DRAFT_PR,
            actor=actor,
            summary=f"{batch.title} ({proposal.changed_lines} lines)",
            branch=proposal.branch,
            target_branch=proposal.base_branch,
            repo=repo_path,
            payload={"body": proposal.body},
        )
        opener = open_pr or _default_open_pr

        def _execute(_req: ActionRequest, _p: ChangeProposal = proposal) -> str:
            return opener(_p)

        guard.guard(req, _execute)
    return proposals


def _default_open_pr(proposal: ChangeProposal) -> str:
    # Thin seam; a real run invokes the GitHub/GitLab MCP create-PR tool.
    return f"draft PR from {proposal.branch} -> {proposal.base_branch}"


def _skipped_record(actor: str, batch: ChangeBatch, proposal: ChangeProposal) -> Any:
    from overnight_eng.models import ActionRecord

    req = ActionRequest(
        action=ActionType.OPEN_DRAFT_PR, actor=actor, summary=batch.title,
        branch=proposal.branch, target_branch=proposal.base_branch,
    )
    return ActionRecord(
        request=req, allowed=False,
        reason=proposal.rejected_reason or "checks failed", executed=False,
    )
