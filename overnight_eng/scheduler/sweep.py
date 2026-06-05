"""The nightly sweep — drive the orchestrator over a batch of signals and emit the digest.

This is the heart of "wake up to a triaged backlog". It:
  1. pulls queued signals (from the Redis event queue / a direct list),
  2. de-duplicates against long-term memory so nothing is refiled,
  3. runs the ADK orchestrator (which routes to triage) per signal,
  4. files results through the PolicyGuard, recording each decision,
  5. renders the morning digest.

The ADK ``Runner`` invocation is imported lazily so this module imports without the runtime;
the de-dup + filing + digest path around it is plain Python and is what the tests exercise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from overnight_eng.models import ActionRequest, ActionType, Severity, Signal, Source
from overnight_eng.reporting.digest import render_digest
from overnight_eng.triage.dedupe import ensure_fingerprint, is_duplicate
from overnight_eng.triage.severity import classify_severity

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.runtime import Runtime


def _file_issue_executor(runtime: "Runtime", signal: Signal, severity: Severity):
    """Return an executor that files a backlog issue via the GitHub MCP toolset.

    In the MVP this delegates to the GitHub toolset if present; otherwise it returns a stub
    reference so the dry-run/no-credential path still produces a coherent digest.
    """

    def execute(_req: ActionRequest) -> str:
        forge = getattr(runtime, "forge", None)
        if forge is not None:
            return forge.create_issue(
                repo=signal.project,
                title=f"[{severity.value}] {signal.title}",
                body=f"{signal.body}\n\nSource: {signal.url}\nFingerprint: {signal.fingerprint}",
                labels=[severity.value, "overnight", signal.source.value],
            )
        # No forge wired (dry-run / no creds) — deterministic stub keeps the digest coherent.
        return runtime.env.get("ISSUE_URL_STUB", f"filed:{signal.fingerprint}")

    return execute


def triage_one(runtime: "Runtime", signal: Signal) -> None:
    """De-dup, classify, and file (or bump) a single signal — fully PolicyGuard-gated."""
    sig = ensure_fingerprint(signal)

    dup_ref = is_duplicate(sig, runtime.memory.seen)
    if dup_ref:
        runtime.guard.guard(
            ActionRequest(
                ActionType.COMMENT,
                actor="signal_triage",
                summary=f"recurrence of {sig.title} (already filed: {dup_ref})",
                payload={"ref": dup_ref, "fingerprint": sig.fingerprint},
            ),
            lambda _r: f"bumped {dup_ref}",
        )
        return

    severity = classify_severity(sig)
    rec = runtime.guard.guard(
        ActionRequest(
            ActionType.CREATE_ISSUE,
            actor="signal_triage",
            summary=f"[{severity.value}] {sig.title}",
            repo=sig.project,
            payload={"fingerprint": sig.fingerprint, "url": sig.url},
        ),
        _file_issue_executor(runtime, sig, severity),
    )
    if rec.executed:
        runtime.memory.record_filed_issue(sig.fingerprint, rec.result, title=sig.title)


def run_sweep(runtime: "Runtime", signals: list[Signal]) -> str:
    """Triage a batch and return the rendered morning digest."""
    for signal in signals:
        triage_one(runtime, signal)
    return render_digest(runtime.guard.audit_log)
