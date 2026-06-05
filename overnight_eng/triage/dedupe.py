"""De-duplication so the fleet does not refile the same issue every night.

The orchestrator keeps a ``SeenStore`` of fingerprints it has already acted on (backed by
Mem0/Postgres in production; an in-memory set in tests). Before filing a backlog issue, the
triage agent checks the store; recurring problems get a comment/bump instead of a duplicate.

Fingerprinting is intentionally pure and deterministic so "is this the same problem?" is
testable without any model call. Upstream fingerprints (Sentry already groups issues) are
preferred; we only synthesize one when the source doesn't provide a stable id.
"""

from __future__ import annotations

import hashlib
import re

from overnight_eng.models import Signal, Source

_WS = re.compile(r"\s+")
# Volatile tokens that would otherwise defeat grouping: hex ids, line/col numbers, uuids,
# memory addresses, timestamps-ish digit runs.
_VOLATILE = re.compile(
    r"(0x[0-9a-fA-F]+"          # hex / pointers
    r"|[0-9a-f]{8,}"            # long hex blobs / uuids without dashes
    r"|:\d+:\d+"               # :line:col
    r"|\d{3,})"               # long digit runs (incl. unit-suffixed, e.g. 5000ms)
)


def normalize_title(title: str) -> str:
    """Collapse whitespace and mask volatile tokens so near-identical errors group together."""
    masked = _VOLATILE.sub("#", title.strip().lower())
    return _WS.sub(" ", masked)


def compute_fingerprint(source: Source, title: str, project: str = "") -> str:
    """Deterministic fingerprint for a signal lacking an upstream stable id."""
    basis = f"{source.value}|{project.strip().lower()}|{normalize_title(title)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def ensure_fingerprint(signal: Signal) -> Signal:
    """Return a Signal guaranteed to carry a stable fingerprint.

    If the upstream system supplied one (e.g. Sentry's issue group), keep it; otherwise
    synthesize one. Frozen dataclass -> return a copy when we have to fill it in.
    """
    if signal.fingerprint:
        return signal
    from dataclasses import replace

    fp = compute_fingerprint(signal.source, signal.title, signal.project)
    return replace(signal, fingerprint=fp)


class SeenStore:
    """Minimal interface the orchestrator depends on; swap for Mem0/Postgres in prod.

    ``seen`` maps fingerprint -> the backlog item key (issue url/id) we created for it, so a
    recurrence can comment on the existing issue rather than open a new one.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._seen: dict[str, str] = dict(initial or {})

    def is_known(self, fingerprint: str) -> bool:
        return fingerprint in self._seen

    def reference_for(self, fingerprint: str) -> str | None:
        return self._seen.get(fingerprint)

    def remember(self, fingerprint: str, backlog_ref: str) -> None:
        self._seen.setdefault(fingerprint, backlog_ref)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._seen)


def is_duplicate(signal: Signal, store: SeenStore) -> str | None:
    """Return the existing backlog reference if this signal was already filed, else ``None``."""
    sig = ensure_fingerprint(signal)
    if store.is_known(sig.fingerprint):
        return store.reference_for(sig.fingerprint) or sig.fingerprint
    return None
