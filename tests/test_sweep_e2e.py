"""End-to-end MVP verification: Sentry signal -> triaged backlog issue, with de-dup.

Exercises the real PolicyGuard + FleetMemory + dedupe + severity + digest path with a
lightweight stand-in Runtime (no ADK/forge needed). This is the test the plan's verification
section describes: one issue filed, a duplicate delivery files nothing new.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from overnight_eng.memory.store import FleetMemory
from overnight_eng.models import ActionType
from overnight_eng.sources.sentry import normalize_sentry
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
from overnight_eng.scheduler.sweep import run_sweep, triage_one

FIXTURE = Path(__file__).parent / "fixtures" / "sentry_issue_alert.json"


@dataclass
class _FakeEnv:
    _vals: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return self._vals.get(key, default)


@dataclass
class _FakeRuntime:
    """Just the surface ``sweep`` touches."""

    guard: PolicyGuard
    memory: FleetMemory
    toolsets: dict[str, Any]
    env: _FakeEnv


def _runtime(dry_run: bool = False) -> _FakeRuntime:
    return _FakeRuntime(
        guard=PolicyGuard(config=PolicyConfig(dry_run=dry_run)),
        memory=FleetMemory.create(),
        toolsets={},  # no github toolset -> executor returns a deterministic stub ref
        env=_FakeEnv({"ISSUE_URL_STUB": "https://github.com/o/r/issues/101"}),
    )


def _sentry_signal():
    return normalize_sentry(json.loads(FIXTURE.read_text()))


def test_sweep_files_exactly_one_issue() -> None:
    rt = _runtime()
    digest = run_sweep(rt, [_sentry_signal()])
    created = [r for r in rt.guard.audit_log if r.request.action is ActionType.CREATE_ISSUE]
    assert len(created) == 1
    assert created[0].executed
    assert "Issues filed" in digest


def test_duplicate_delivery_files_no_second_issue() -> None:
    rt = _runtime()
    triage_one(rt, _sentry_signal())          # night 1 / first delivery
    triage_one(rt, _sentry_signal())          # same Sentry issue again
    created = [r for r in rt.guard.audit_log if r.request.action is ActionType.CREATE_ISSUE]
    comments = [r for r in rt.guard.audit_log if r.request.action is ActionType.COMMENT]
    assert len(created) == 1            # filed once
    assert len(comments) == 1           # second time -> bump comment, not a new issue


def test_memory_remembers_fingerprint_across_sweeps() -> None:
    rt = _runtime()
    triage_one(rt, _sentry_signal())
    sig = _sentry_signal()
    assert rt.memory.seen.is_known(sig.fingerprint)


def test_dry_run_sweep_has_zero_side_effects() -> None:
    rt = _runtime(dry_run=True)
    digest = run_sweep(rt, [_sentry_signal()])
    # nothing executed; the issue shows up only as a proposed (dry-run) action
    assert all(not r.executed for r in rt.guard.audit_log)
    assert "Proposed (dry-run" in digest
    # and because nothing executed, memory did not record the fingerprint
    assert not rt.memory.seen.is_known(_sentry_signal().fingerprint)
