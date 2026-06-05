"""Tests for the deterministic severity heuristic."""

from __future__ import annotations

from overnight_eng.models import Severity, Signal, Source
from overnight_eng.triage.severity import classify_severity


def _sig(title: str, level: str = "", body: str = "") -> Signal:
    return Signal(source=Source.SENTRY, title=title, body=body,
                  metadata={"level": level} if level else {})


def test_fatal_level_is_critical() -> None:
    assert classify_severity(_sig("anything", level="fatal")) is Severity.CRITICAL


def test_warning_level_is_medium() -> None:
    assert classify_severity(_sig("some warning", level="warning")) is Severity.MEDIUM


def test_outage_keyword_is_critical() -> None:
    assert classify_severity(_sig("Production outage in checkout")) is Severity.CRITICAL


def test_security_error_escalates_above_plain_error() -> None:
    # error-level + security keyword -> CRITICAL, not just HIGH
    assert classify_severity(_sig("security breach detected", level="error")) is Severity.CRITICAL


def test_timeout_is_high() -> None:
    assert classify_severity(_sig("TimeoutError on /api")) is Severity.HIGH


def test_plain_error_level_without_keywords_is_high() -> None:
    assert classify_severity(_sig("weird thing happened", level="error")) is Severity.HIGH


def test_unknown_defaults_medium() -> None:
    assert classify_severity(_sig("a routine event")) is Severity.MEDIUM
