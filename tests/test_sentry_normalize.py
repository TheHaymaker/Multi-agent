"""Tests for the Sentry payload normalizer + the dedupe integration it feeds."""

from __future__ import annotations

import json
from pathlib import Path

from overnight_eng.models import Source
from overnight_eng.sources.sentry import normalize_sentry
from overnight_eng.triage.dedupe import SeenStore, ensure_fingerprint, is_duplicate

FIXTURE = Path(__file__).parent / "fixtures" / "sentry_issue_alert.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_normalizes_core_fields() -> None:
    sig = normalize_sentry(_payload())
    assert sig.source is Source.SENTRY
    assert sig.title.startswith("TimeoutError")
    assert sig.project == "checkout"
    assert sig.url.endswith("/1234567890/")
    assert sig.metadata.get("level") == "error"


def test_reuses_sentry_issue_id_as_fingerprint() -> None:
    sig = normalize_sentry(_payload())
    assert sig.fingerprint == "1234567890"  # Sentry's grouping, not synthesized


def test_handles_top_level_message_payload() -> None:
    sig = normalize_sentry({"message": "Something broke", "project_slug": "api"})
    assert sig.title == "Something broke"
    assert sig.project == "api"
    assert sig.fingerprint == ""  # nothing stable -> synthesized downstream


def test_duplicate_detection_across_two_deliveries() -> None:
    store = SeenStore()
    sig = normalize_sentry(_payload())
    assert is_duplicate(sig, store) is None
    store.remember(ensure_fingerprint(sig).fingerprint, "ISSUE-CHECKOUT-7F")
    # Same Sentry issue delivered again -> recognized, not refiled.
    assert is_duplicate(normalize_sentry(_payload()), store) == "ISSUE-CHECKOUT-7F"
