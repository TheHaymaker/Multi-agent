"""Command-line entrypoint: run sweeps, serve ingress, check health.

    overnight health                 # verify config + which MCP toolsets connect
    overnight triage <payload.json>  # normalize a Sentry payload and triage it once (dry-run safe)
    overnight sweep                  # drain the queue, triage, print the morning digest
    overnight serve                  # run the webhook ingress + APScheduler (overnight daemon)

Heavy runtime pieces are imported lazily inside each command so ``overnight --help`` works in
a bare environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_health(_args: argparse.Namespace) -> int:
    from overnight_eng.app_utils.env import init_environment
    from overnight_eng.app_utils.env import REQUIRED_SECRET_KEYS

    env = init_environment()
    print(f"postgres_dsn : {env.settings.postgres_dsn}")
    print(f"redis_url    : {env.settings.redis_url}")
    print(f"qdrant_url   : {env.settings.qdrant_url}")
    print(f"langfuse     : {env.settings.langfuse_host}")
    print(f"reasoning    : {env.settings.reasoning_model}")
    print(f"surgeon      : {env.settings.surgeon_model} "
          f"(subscription={env.settings.surgeon_use_subscription})")
    print(f"dry_run      : {env.settings.dry_run}")
    missing = [k for k in REQUIRED_SECRET_KEYS if not env.get(k)]
    if missing:
        print(f"MISSING required secrets: {', '.join(missing)}")
        return 1
    print("required secrets: present ✅")
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    from overnight_eng.sources.sentry import normalize_sentry
    from overnight_eng.memory.store import FleetMemory
    from overnight_eng.tools.policy_guard import PolicyConfig, PolicyGuard
    from overnight_eng.scheduler.sweep import triage_one
    from overnight_eng.reporting.digest import render_digest

    payload = json.loads(Path(args.payload).read_text())
    signal = normalize_sentry(payload)

    # Minimal local runtime (no ADK/forge) — dry-run by default for a safe one-shot.
    class _Env:
        def get(self, k: str, d: str = "") -> str:
            return d

    class _RT:
        guard = PolicyGuard(config=PolicyConfig(dry_run=not args.execute))
        memory = FleetMemory.create()
        toolsets: dict = {}
        env = _Env()

    rt = _RT()
    triage_one(rt, signal)
    print(render_digest(rt.guard.audit_log))
    return 0


def _cmd_sweep(_args: argparse.Namespace) -> int:
    from overnight_eng.runtime import Runtime
    from overnight_eng.scheduler.jobs import nightly_sweep

    runtime = Runtime.build()
    print(nightly_sweep(runtime))
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    import uvicorn

    from overnight_eng.runtime import Runtime
    from overnight_eng.scheduler.jobs import build_scheduler
    from overnight_eng.scheduler.webhook import create_app

    runtime = Runtime.build()
    scheduler = build_scheduler(runtime)
    scheduler.start()
    print("scheduler started (poll + nightly digest); serving ingress on :8080")
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="overnight", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="verify config + secrets").set_defaults(func=_cmd_health)

    t = sub.add_parser("triage", help="triage a single Sentry payload (dry-run unless --execute)")
    t.add_argument("payload", help="path to a Sentry webhook JSON payload")
    t.add_argument("--execute", action="store_true", help="actually act (default: dry-run)")
    t.set_defaults(func=_cmd_triage)

    sub.add_parser("sweep", help="drain queue, triage, print digest").set_defaults(func=_cmd_sweep)
    sub.add_parser("serve", help="run ingress + scheduler daemon").set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
