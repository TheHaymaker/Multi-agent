"""Scheduling — periodic polls + the nightly full sweep (APScheduler).

For the MVP a lightweight scheduler is plenty; the plan notes Temporal/Prefect as the
scale-up for durable, retryable workflows. This wires:
  * a frequent **poll** (drain the Redis queue, triage event-driven),
  * a **nightly sweep** (full pass; compose + deliver the morning digest).

APScheduler/Redis import lazily so the module imports without them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from overnight_eng.models import Signal, Source

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.runtime import Runtime


def _dict_to_signal(d: dict[str, Any]) -> Signal:
    return Signal(
        source=Source(d.get("source", "sentry")),
        title=d.get("title", ""),
        fingerprint=d.get("fingerprint", ""),
        body=d.get("body", ""),
        url=d.get("url", ""),
        project=d.get("project", ""),
        metadata=d.get("metadata", {}) or {},
    )


def collect_queued_signals(runtime: "Runtime") -> list[Signal]:
    """Drain the Redis queue into Signals (empty list if Redis is unavailable)."""
    try:
        import redis as redis_lib

        from overnight_eng.scheduler.webhook import drain_signals

        client = redis_lib.from_url(runtime.env.settings.redis_url, decode_responses=True)
        return [_dict_to_signal(d) for d in drain_signals(client)]
    except Exception:
        return []


def nightly_sweep(runtime: "Runtime", deliver: Any | None = None) -> str:
    """Run the full sweep over queued signals and deliver the digest."""
    from overnight_eng.scheduler.sweep import run_sweep

    signals = collect_queued_signals(runtime)
    digest = run_sweep(runtime, signals)
    if deliver is not None:
        deliver(digest)
    else:
        _deliver_digest(runtime, digest)
    return digest


def _deliver_digest(runtime: "Runtime", digest: str) -> None:
    """Post the digest to Slack if configured, else print it."""
    url = runtime.env.get("SLACK_WEBHOOK_URL")
    if url:
        try:
            import httpx

            httpx.post(url, json={"text": digest}, timeout=10.0)
            return
        except Exception:
            pass
    print(digest)


def build_scheduler(runtime: "Runtime") -> Any:
    """Construct an APScheduler with the poll + nightly jobs (not started)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BackgroundScheduler()
    # Frequent event-driven poll (drain queue, triage as things arrive).
    sched.add_job(
        lambda: nightly_sweep(runtime),
        IntervalTrigger(minutes=int(runtime.env.get("POLL_MINUTES", "15"))),
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    # Nightly full sweep + digest at 06:00 local.
    sched.add_job(
        lambda: nightly_sweep(runtime),
        CronTrigger(hour=int(runtime.env.get("DIGEST_HOUR", "6"))),
        id="nightly",
        max_instances=1,
        coalesce=True,
    )
    return sched
