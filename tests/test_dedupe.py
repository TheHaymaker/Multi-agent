"""Tests for triage de-duplication — proves the fleet won't refile the same issue nightly."""

from __future__ import annotations

from overnight_eng.models import Signal, Source
from overnight_eng.triage.dedupe import (
    SeenStore,
    compute_fingerprint,
    ensure_fingerprint,
    is_duplicate,
    normalize_title,
)


def test_normalize_masks_volatile_tokens() -> None:
    a = normalize_title("NullPointer at user.py:128:14 (0xdeadbeef)")
    b = normalize_title("NullPointer at user.py:512:3 (0xfeedface)")
    assert a == b  # line/col + pointer masked away -> same group


def test_fingerprint_stable_and_groups_similar() -> None:
    fp1 = compute_fingerprint(Source.SENTRY, "TimeoutError on /api/orders id=99213", "checkout")
    fp2 = compute_fingerprint(Source.SENTRY, "TimeoutError on /api/orders id=10544", "checkout")
    assert fp1 == fp2


def test_fingerprint_differs_across_projects() -> None:
    assert compute_fingerprint(Source.SENTRY, "boom", "a") != compute_fingerprint(
        Source.SENTRY, "boom", "b"
    )


def test_ensure_fingerprint_preserves_upstream_id() -> None:
    sig = Signal(source=Source.SENTRY, title="x", fingerprint="UPSTREAM-123")
    assert ensure_fingerprint(sig).fingerprint == "UPSTREAM-123"


def test_ensure_fingerprint_synthesizes_when_missing() -> None:
    sig = Signal(source=Source.GRAFANA, title="cpu > 90%", fingerprint="", project="api")
    out = ensure_fingerprint(sig)
    assert out.fingerprint and out.fingerprint == compute_fingerprint(
        Source.GRAFANA, "cpu > 90%", "api"
    )


def test_is_duplicate_detects_known_signal() -> None:
    store = SeenStore()
    sig = Signal(source=Source.SENTRY, title="boom", fingerprint="FP1")
    assert is_duplicate(sig, store) is None  # first sighting

    store.remember("FP1", "https://github.com/o/r/issues/7")
    assert is_duplicate(sig, store) == "https://github.com/o/r/issues/7"


def test_recurring_synthesized_signal_is_duplicate_next_night() -> None:
    store = SeenStore()
    night1 = Signal(source=Source.SENTRY, title="DB pool exhausted after 5000ms", project="api")
    ref = is_duplicate(night1, store)
    assert ref is None
    store.remember(ensure_fingerprint(night1).fingerprint, "ISSUE-1")

    # Next night: same class of error, different volatile numbers -> recognized as dup.
    night2 = Signal(source=Source.SENTRY, title="DB pool exhausted after 8123ms", project="api")
    assert is_duplicate(night2, store) == "ISSUE-1"


def test_remember_is_idempotent() -> None:
    store = SeenStore()
    store.remember("FP", "first")
    store.remember("FP", "second")
    assert store.reference_for("FP") == "first"
