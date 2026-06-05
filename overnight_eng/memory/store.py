"""Cross-night memory — the local equivalent of Dev Signal's Vertex AI Memory Bank.

Two jobs, mirroring the series' ``PreloadMemoryTool`` / ``LoadMemoryTool`` + persist callback:

  * **Preload / recall** — at the start of a sweep the orchestrator pulls relevant memories
    ("you decided not to refile flaky test X", "prefer terse PR descriptions") so behavior is
    personalized and stable across nights.
  * **Persist** — after each agent turn, salient facts and the fingerprints of filed issues
    are written back, so tomorrow's run remembers what it already did.

Backed by **Mem0** (MIT) over **pgvector/Qdrant** in production. If Mem0 isn't installed (or
for tests), it transparently falls back to an in-process store so the rest of the system runs
unchanged — the same local-vs-cloud binding-swap pattern the series demonstrates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from overnight_eng.triage.dedupe import SeenStore

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment


class MemoryBackend(Protocol):
    """Minimal surface the fleet needs; satisfied by Mem0 and the fallback alike."""

    def add(self, text: str, *, user_id: str, metadata: dict[str, Any] | None = None) -> None: ...
    def search(self, query: str, *, user_id: str, limit: int = 5) -> list[str]: ...


class _InMemoryBackend:
    """Dependency-free fallback. Keyword overlap instead of embeddings — good enough for dev."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, str]] = []  # (user_id, text)

    def add(self, text: str, *, user_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._rows.append((user_id, text))

    def search(self, query: str, *, user_id: str, limit: int = 5) -> list[str]:
        terms = {t for t in query.lower().split() if len(t) > 2}
        scored = []
        for uid, text in self._rows:
            if uid != user_id:
                continue
            overlap = len(terms & set(text.lower().split()))
            if overlap:
                scored.append((overlap, text))
        scored.sort(reverse=True)
        return [t for _, t in scored[:limit]]


def _build_mem0(env: "Environment") -> MemoryBackend | None:
    try:
        from mem0 import Memory  # type: ignore
    except Exception:
        return None
    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {"url": env.settings.qdrant_url, "collection_name": "overnight_eng"},
        },
        "llm": {"provider": "litellm", "config": {"model": env.settings.reasoning_model}},
    }
    try:
        return Memory.from_config(config)  # type: ignore[no-any-return]
    except Exception:
        return None


class FleetMemory:
    """Long-term memory + persistent de-dup, shared by the orchestrator and specialists."""

    def __init__(self, backend: MemoryBackend, user_id: str = "overnight") -> None:
        self._backend = backend
        self._user_id = user_id
        self.seen = SeenStore()  # hot cache of filed fingerprints; hydrated from backend below
        self._hydrate_seen()

    @classmethod
    def create(cls, env: "Environment | None" = None) -> "FleetMemory":
        backend: MemoryBackend = _InMemoryBackend()
        if env is not None:
            backend = _build_mem0(env) or backend
        return cls(backend)

    # ----- recall / persist (the preload + after_agent_callback equivalents) -------

    def preload(self, context: str, limit: int = 5) -> list[str]:
        """Relevant memories to inject at the start of a sweep/turn."""
        return self._backend.search(context, user_id=self._user_id, limit=limit)

    def remember_fact(self, text: str, metadata: dict[str, Any] | None = None) -> None:
        self._backend.add(text, user_id=self._user_id, metadata=metadata or {})

    # ----- de-dup persistence ------------------------------------------------------

    def _hydrate_seen(self) -> None:
        for text in self._backend.search("filed-issue fingerprint", user_id=self._user_id, limit=500):
            # convention: "filed-issue <fingerprint> <ref>"
            parts = text.split()
            if len(parts) >= 3 and parts[0] == "filed-issue":
                self.seen.remember(parts[1], parts[2])

    def record_filed_issue(self, fingerprint: str, ref: str, title: str = "") -> None:
        """Mark a fingerprint as filed so it is not refiled on a later night."""
        self.seen.remember(fingerprint, ref)
        self.remember_fact(
            f"filed-issue {fingerprint} {ref}",
            metadata={"kind": "filed_issue", "title": title},
        )
