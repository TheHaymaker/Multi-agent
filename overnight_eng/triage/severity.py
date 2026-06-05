"""Deterministic severity heuristic.

The triage *agent* uses Claude for nuanced root-cause reasoning, but a cheap, deterministic
baseline is valuable: it gives the agent a prior to anchor on, lets the system degrade
gracefully if the model is unavailable, and is unit-testable. The agent may override it.
"""

from __future__ import annotations

from overnight_eng.models import Severity, Signal

# Substrings that bump severity, most → least severe. First match wins.
_RULES: tuple[tuple[Severity, tuple[str, ...]], ...] = (
    (Severity.CRITICAL, ("outage", "data loss", "corruption", "security", "cannot login",
                         "payment fail", "5xx spike", "oomkilled", "deadlock")),
    (Severity.HIGH, ("timeout", "exception", "error", "500", "crash", "unhandled",
                    "latency", "p99", "memory leak")),
    (Severity.MEDIUM, ("warning", "deprecated", "retry", "slow", "degraded", "flaky")),
    (Severity.LOW, ("notice", "info", "debug")),
)


def classify_severity(signal: Signal) -> Severity:
    """Best-effort severity from the signal's text + source hints.

    Honors an explicit upstream level (Sentry ``level=fatal/error/warning``) when present,
    else falls back to keyword rules, else MEDIUM.
    """
    level = str(signal.metadata.get("level", "")).lower()
    if level in ("fatal", "critical"):
        return Severity.CRITICAL
    if level == "error":
        # error-level still scans keywords so e.g. a security error -> CRITICAL.
        pass
    elif level == "warning":
        return Severity.MEDIUM

    haystack = f"{signal.title}\n{signal.body}".lower()
    for severity, needles in _RULES:
        if any(n in haystack for n in needles):
            return severity

    if level == "error":
        return Severity.HIGH
    return Severity.MEDIUM
