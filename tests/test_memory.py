"""Tests for FleetMemory's in-memory fallback + de-dup persistence/hydration."""

from __future__ import annotations

from overnight_eng.memory.store import FleetMemory


def test_remember_and_recall() -> None:
    mem = FleetMemory.create()  # no env -> in-memory backend
    mem.remember_fact("prefer terse PR descriptions for the checkout service")
    hits = mem.preload("how should PR descriptions for checkout look")
    assert any("terse" in h for h in hits)


def test_filed_issue_round_trips_into_seen() -> None:
    mem = FleetMemory.create()
    mem.record_filed_issue("FP-123", "https://github.com/o/r/issues/9", title="timeout")
    assert mem.seen.is_known("FP-123")
    assert mem.seen.reference_for("FP-123") == "https://github.com/o/r/issues/9"


def test_seen_hydrates_from_backend_on_new_instance() -> None:
    mem = FleetMemory.create()
    mem.record_filed_issue("FP-XYZ", "ISSUE-1")
    # A fresh FleetMemory over the SAME backend should rehydrate the filed fingerprint.
    rehydrated = FleetMemory(mem._backend)
    assert rehydrated.seen.is_known("FP-XYZ")
