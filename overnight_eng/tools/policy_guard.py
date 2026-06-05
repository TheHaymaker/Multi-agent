"""PolicyGuard — the single gate every side-effecting action passes through.

Under **propose-only autonomy** the fleet may file issues, comment, work on its *own*
branches, rebase them onto main, and open/update **draft** PRs — but it must never push to
a protected branch, merge, force-push, close other people's work, or delete branches.

Design goals:
  * **One choke point.** No agent calls an MCP/git write directly; it builds an
    :class:`ActionRequest` and asks the guard. This makes the safety policy auditable and
    testable in isolation (stdlib-only, no agent runtime needed).
  * **Fail closed.** Unknown actions, missing branch info, or protected-branch targets are
    denied, not allowed.
  * **Dry-run.** In dry-run mode the guard records what *would* happen and reports the
    action as not executed, enabling a zero-side-effect "what would you do tonight?" sweep.
  * **Audit trail.** Every decision is appended to an in-memory log (persist it wherever).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field

from overnight_eng.models import ActionRecord, ActionRequest, ActionType

# Actions that are always permitted (subject to branch checks below).
_ALLOWED_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.CREATE_ISSUE,
        ActionType.COMMENT,
        ActionType.CREATE_BRANCH,
        ActionType.PUSH_AGENT_BRANCH,
        ActionType.REBASE_AGENT_BRANCH,
        ActionType.OPEN_DRAFT_PR,
        ActionType.UPDATE_DRAFT_PR,
    }
)

# Actions that are categorically denied under propose-only autonomy.
_DENIED_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.PUSH_PROTECTED,
        ActionType.MERGE,
        ActionType.FORCE_PUSH,
        ActionType.CLOSE_OTHERS,
        ActionType.DELETE_BRANCH,
    }
)

# Actions whose target branch must be an agent-owned (not protected) branch.
_BRANCH_SCOPED_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.CREATE_BRANCH,
        ActionType.PUSH_AGENT_BRANCH,
        ActionType.REBASE_AGENT_BRANCH,
        ActionType.OPEN_DRAFT_PR,
        ActionType.UPDATE_DRAFT_PR,
    }
)


@dataclass
class PolicyConfig:
    """Tunable policy. Defaults encode the approved propose-only posture."""

    # Branch globs the fleet is allowed to write to (must NOT overlap protected_branches).
    agent_branch_globs: tuple[str, ...] = ("agent/*", "overnight/*", "bot/*")
    # Branches that may never be pushed to, merged into by the bot, or deleted.
    protected_branches: tuple[str, ...] = ("main", "master", "develop", "release/*", "prod*")
    dry_run: bool = False


@dataclass
class PolicyDecision:
    """Result of evaluating a single :class:`ActionRequest`."""

    allowed: bool
    reason: str


@dataclass
class PolicyGuard:
    """Evaluates and (optionally) executes actions under the propose-only policy."""

    config: PolicyConfig = field(default_factory=PolicyConfig)
    audit_log: list[ActionRecord] = field(default_factory=list)

    # ----- evaluation (pure, no side effects) --------------------------------------

    def _is_protected(self, branch: str | None) -> bool:
        if not branch:
            return False
        return any(fnmatch.fnmatch(branch, pat) for pat in self.config.protected_branches)

    def _is_agent_owned(self, branch: str | None) -> bool:
        if not branch:
            return False
        return any(fnmatch.fnmatch(branch, pat) for pat in self.config.agent_branch_globs)

    def evaluate(self, req: ActionRequest) -> PolicyDecision:
        """Decide whether ``req`` is permitted. Fails closed."""
        action = req.action
        name = getattr(action, "value", action)  # robust even if a raw string slips through

        if action in _DENIED_ACTIONS:
            return PolicyDecision(False, f"action '{name}' is denied under propose-only autonomy")

        if action not in _ALLOWED_ACTIONS:
            return PolicyDecision(False, f"unknown action '{name}' — denied by default")

        if action in _BRANCH_SCOPED_ACTIONS:
            branch = req.branch
            if not branch:
                return PolicyDecision(False, f"'{action.value}' requires a branch; none provided")
            if self._is_protected(branch):
                return PolicyDecision(False, f"branch '{branch}' is protected — refusing to write")
            if not self._is_agent_owned(branch):
                return PolicyDecision(
                    False,
                    f"branch '{branch}' is not agent-owned "
                    f"(expected one of {self.config.agent_branch_globs})",
                )

        # Draft PRs / rebases must target a protected base (you review there), never write to it.
        if action in (ActionType.OPEN_DRAFT_PR, ActionType.UPDATE_DRAFT_PR, ActionType.REBASE_AGENT_BRANCH):
            if req.target_branch and not self._is_protected(req.target_branch):
                # A non-protected base is allowed but unusual; surface it in the reason.
                return PolicyDecision(True, f"allowed (base '{req.target_branch}' is not a protected branch)")

        return PolicyDecision(True, "allowed under propose-only policy")

    # ----- execution (gated; dry-run aware) ----------------------------------------

    def guard(
        self,
        req: ActionRequest,
        execute: Callable[[ActionRequest], str] | None = None,
    ) -> ActionRecord:
        """Evaluate ``req``; if allowed and not a dry-run, run ``execute`` and record it.

        ``execute`` is the thing that actually performs the MCP/git call. Keeping it as an
        injected callback means the guard stays testable without any real side effects, and
        every executor in the codebase is forced through this gate.
        """
        decision = self.evaluate(req)
        record = ActionRecord(
            request=req,
            allowed=decision.allowed,
            reason=decision.reason,
            dry_run=self.config.dry_run,
        )

        if decision.allowed and not self.config.dry_run and execute is not None:
            try:
                record.result = execute(req)
                record.executed = True
            except Exception as exc:  # noqa: BLE001 — record, don't crash the nightly sweep
                record.executed = False
                record.result = f"executor error: {exc!r}"

        self.audit_log.append(record)
        return record

    # ----- reporting ---------------------------------------------------------------

    def summary(self) -> dict[str, int]:
        """Counts for the morning digest."""
        allowed = sum(1 for r in self.audit_log if r.allowed)
        executed = sum(1 for r in self.audit_log if r.executed)
        denied = sum(1 for r in self.audit_log if not r.allowed)
        return {
            "total": len(self.audit_log),
            "allowed": allowed,
            "executed": executed,
            "denied": denied,
        }
