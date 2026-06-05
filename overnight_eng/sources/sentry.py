"""Normalize Sentry issue/event webhook payloads into :class:`Signal` objects.

Kept pure (dict in, Signal out) so the MVP triage vertical can be verified end-to-end with a
recorded fixture and no live Sentry. Sentry already groups events into *issues* and exposes a
stable per-issue identity; we reuse it as the fingerprint so de-dup matches Sentry's grouping.

Sentry has several payload shapes (legacy ``event_alert``/``issue_alert`` plugins and the
newer "Issues" integration resource). This adapter handles the common ones defensively and
falls back to a synthesized fingerprint when no stable id is present.
"""

from __future__ import annotations

from typing import Any

from overnight_eng.models import Signal, Source


def _first(*vals: Any) -> str:
    for v in vals:
        if v:
            return str(v)
    return ""


def _extract_issue(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the issue-ish object out of the several shapes Sentry sends."""
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("issue", "event", "error"):
            inner = data.get(key)
            if isinstance(inner, dict):
                return inner
    # Some plugin payloads put fields at the top level.
    return data if isinstance(data, dict) else {}


def normalize_sentry(payload: dict[str, Any]) -> Signal:
    """Convert a Sentry webhook payload into a normalized Signal.

    Fingerprint precedence: explicit ``issue.id`` / ``group_id`` / ``culprit`` from Sentry,
    else left empty for :func:`overnight_eng.triage.dedupe.ensure_fingerprint` to synthesize.
    """
    issue = _extract_issue(payload)

    title = _first(
        issue.get("title"),
        issue.get("metadata", {}).get("value") if isinstance(issue.get("metadata"), dict) else None,
        issue.get("message"),
        payload.get("message"),
        "Untitled Sentry issue",
    )

    project = _first(
        issue.get("project"),
        (payload.get("project") if isinstance(payload.get("project"), str) else None),
        payload.get("project_slug"),
    )

    url = _first(issue.get("web_url"), issue.get("url"), payload.get("url"))

    # Stable upstream identity -> reuse Sentry's grouping for de-dup.
    fingerprint = _first(
        issue.get("id"),
        issue.get("group_id"),
        issue.get("shortId"),
    )

    level = _first(issue.get("level"), payload.get("level")).lower()
    culprit = _first(issue.get("culprit"))
    count = issue.get("count")
    body_parts = [p for p in (culprit, f"events={count}" if count else "", url) if p]

    return Signal(
        source=Source.SENTRY,
        title=title.strip(),
        fingerprint=fingerprint,
        body=" | ".join(body_parts),
        url=url,
        project=project,
        metadata={"level": level} if level else {},
    )
