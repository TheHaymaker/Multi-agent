"""Overnight Engineering — a local-first multi-agent system.

A fleet of engineering agents that run on your machine overnight: triaging Sentry/Grafana
signals, polling Jira, coordinating PRs, and proposing code-quality / test / performance /
TypeScript-type improvements — then handing you a morning digest.

The orchestration spine is Google ADK (open source, model-agnostic) so the same agent code
runs locally (Plan A) or on Google Cloud (Plan B); only the service bindings differ.
"""

__version__ = "0.1.0"
