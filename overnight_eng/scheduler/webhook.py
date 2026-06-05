"""Event ingress — a small FastAPI app that turns inbound webhooks into queued Signals.

Sentry alerts and GitHub/GitLab PR events POST here; each is normalized into a
:class:`Signal` and pushed onto the Redis queue the nightly sweep (and event-driven
reactions) drain. Kept thin: parsing/normalization lives in ``sources/``; this module just
receives, verifies, and enqueues.

FastAPI/Redis are imported lazily so importing the package doesn't require them.
"""

from __future__ import annotations

import json
from typing import Any

from overnight_eng.sources.sentry import normalize_sentry

QUEUE_KEY = "overnight:signals"


def enqueue_signal(redis_client: Any, signal_dict: dict[str, Any]) -> None:
    redis_client.rpush(QUEUE_KEY, json.dumps(signal_dict))


def drain_signals(redis_client: Any, limit: int = 1000) -> list[dict[str, Any]]:
    """Pop up to ``limit`` queued signal dicts (used by the nightly sweep)."""
    out: list[dict[str, Any]] = []
    for _ in range(limit):
        raw = redis_client.lpop(QUEUE_KEY)
        if raw is None:
            break
        out.append(json.loads(raw))
    return out


def create_app() -> Any:
    """Build the FastAPI ingress app. Lazy so the import is optional."""
    from fastapi import FastAPI, Request
    import redis as redis_lib

    from overnight_eng.app_utils.env import init_environment

    env = init_environment()
    client = redis_lib.from_url(env.settings.redis_url, decode_responses=True)
    app = FastAPI(title="Overnight Engineering ingress")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/sentry")
    async def sentry_hook(request: Request) -> dict[str, Any]:
        payload = await request.json()
        signal = normalize_sentry(payload)
        enqueue_signal(client, _signal_to_dict(signal))
        return {"queued": True, "fingerprint": signal.fingerprint, "title": signal.title}

    return app


def _signal_to_dict(signal: Any) -> dict[str, Any]:
    return {
        "source": signal.source.value,
        "title": signal.title,
        "fingerprint": signal.fingerprint,
        "body": signal.body,
        "url": signal.url,
        "project": signal.project,
        "metadata": signal.metadata,
    }
