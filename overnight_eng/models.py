"""Core data models shared across the fleet.

Deliberately stdlib-only (``dataclasses`` + ``enum``) so the testable core — models,
``PolicyGuard``, and triage de-duplication — imports and runs with no third-party
dependencies. The ADK / Mem0 / Claude-Agent-SDK integrations import their heavy libraries
lazily, keeping this module portable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Triage severity, ordered low -> critical."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Source(str, Enum):
    """Where a signal originated."""

    SENTRY = "sentry"
    GRAFANA = "grafana"
    JIRA = "jira"
    GITHUB = "github"
    GITLAB = "gitlab"
    QUALITY = "quality"
    TEST = "test"
    PERFORMANCE = "performance"
    TYPESCRIPT = "typescript"


@dataclass(frozen=True)
class Signal:
    """A normalized incoming signal (a Sentry issue, a Grafana alert, etc.).

    ``fingerprint`` is the stable identity used for de-duplication: two signals with the
    same fingerprint are considered the same underlying problem, even across nights.
    """

    source: Source
    title: str
    fingerprint: str = ""  # empty -> synthesized by triage.dedupe.ensure_fingerprint
    body: str = ""
    url: str = ""
    project: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class TriageResult:
    """The triage agent's verdict on a signal."""

    signal: Signal
    severity: Severity
    summary: str
    root_cause_hypothesis: str = ""
    suggested_labels: list[str] = field(default_factory=list)
    duplicate_of: str | None = None  # fingerprint of an existing backlog item, if any

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


class ActionType(str, Enum):
    """Every side-effecting action the fleet can request, routed through ``PolicyGuard``."""

    CREATE_ISSUE = "create_issue"
    COMMENT = "comment"
    CREATE_BRANCH = "create_branch"
    PUSH_AGENT_BRANCH = "push_agent_branch"
    REBASE_AGENT_BRANCH = "rebase_agent_branch"
    OPEN_DRAFT_PR = "open_draft_pr"
    UPDATE_DRAFT_PR = "update_draft_pr"
    # --- denied under propose-only autonomy ---
    PUSH_PROTECTED = "push_protected"
    MERGE = "merge"
    FORCE_PUSH = "force_push"
    CLOSE_OTHERS = "close_others"
    DELETE_BRANCH = "delete_branch"


@dataclass
class ActionRequest:
    """A request to take a side-effecting action.

    ``branch`` / ``target_branch`` let ``PolicyGuard`` verify the action only touches an
    agent-owned branch and never a protected one. ``actor`` is the requesting agent.
    """

    action: ActionType
    actor: str
    summary: str
    branch: str | None = None
    target_branch: str | None = None
    repo: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class ActionRecord:
    """Append-only audit record of a policy decision + (optional) execution outcome."""

    request: ActionRequest
    allowed: bool
    reason: str
    dry_run: bool = False
    executed: bool = False
    result: str = ""
    decided_at: float = field(default_factory=time.time)
