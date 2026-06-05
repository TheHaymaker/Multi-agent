"""Runtime assembly — binds the agent App to its services (Plan A bindings).

This is the single place the local service bindings are chosen:
  * **Session/short-term state** -> ADK ``DatabaseSessionService`` (Postgres)
  * **Long-term memory** -> :class:`FleetMemory` (Mem0/pgvector, in-memory fallback)
  * **Observability** -> Langfuse via OpenTelemetry (ADK already emits OTel spans)

Plan B (GCP) swaps only this module: ``VertexAiSessionService`` + Vertex Memory Bank +
Cloud Trace. The agents, tools, and PolicyGuard are untouched — the local↔cloud binding swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from overnight_eng.app_utils.env import Environment, init_environment
from overnight_eng.memory.store import FleetMemory
from overnight_eng.tools.mcp_config import build_toolsets
from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass
class Runtime:
    """Everything a sweep needs, assembled once per process."""

    env: Environment
    memory: FleetMemory
    guard: PolicyGuard
    toolsets: dict[str, Any]
    surgeon: Any  # CodeSurgeon (Claude Agent SDK worker)
    app: Any  # ADK App (lazily built)
    session_service: Any

    @classmethod
    def build(cls, toolset_names: list[str] | None = None) -> "Runtime":
        env = init_environment()
        memory = FleetMemory.create(env)
        guard = PolicyGuard(config=PolicyConfig(dry_run=env.settings.dry_run))
        toolsets = build_toolsets(env, toolset_names or ["sentry", "grafana", "github"])

        from overnight_eng.workers.code_surgeon import CodeSurgeon

        surgeon = CodeSurgeon(env)

        # Lazy heavy imports: ADK App + Postgres-backed session service.
        from overnight_eng.agents.orchestrator import build_app

        app = build_app(env, memory, toolsets)
        session_service = _build_session_service(env)
        return cls(env, memory, guard, toolsets, surgeon, app, session_service)


def _build_session_service(env: Environment) -> Any:
    """Postgres-backed ADK session service (short-term state + handoff)."""
    from google.adk.sessions import DatabaseSessionService

    return DatabaseSessionService(db_url=env.settings.postgres_dsn)


def init_observability(env: Environment) -> None:
    """Point ADK's OpenTelemetry export at the self-hosted Langfuse OTLP endpoint."""
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return  # observability is best-effort; never block a sweep on it

    provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=f"{env.settings.langfuse_host}/api/public/otel/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
