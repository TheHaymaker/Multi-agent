"""Environment discovery + tiered secret loading.

Mirrors the Dev Signal ``app_utils/env.py`` idea, but swaps Google Secret Manager for a
local-first secret source: a SOPS/age-encrypted ``.env.sops`` decrypted in memory, falling
back to a plain ``.env`` for dev. Secrets are returned as a dict and **not** injected into
``os.environ`` at rest, so the agent process holds them in memory only.

In Plan B (GCP), only ``_fetch_secrets`` changes — point it at Secret Manager and the rest of
the system is untouched, exactly like the local-vs-cloud binding swap the series demonstrates.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Keys the fleet expects to find (documented in .env.example).
REQUIRED_SECRET_KEYS = (
    "ANTHROPIC_API_KEY",      # ADK glue (LiteLlm) — cheap single-shot reasoning
    "GITHUB_TOKEN",           # least-scope bot token for the PR/issue agents
    "SENTRY_AUTH_TOKEN",
)
OPTIONAL_SECRET_KEYS = (
    "GITLAB_TOKEN",
    "GRAFANA_TOKEN",
    "JIRA_API_TOKEN",
    "JIRA_EMAIL",
    "SLACK_WEBHOOK_URL",      # where the morning digest is posted, if set
)


@dataclass
class Settings:
    """Resolved, non-secret configuration for a run."""

    postgres_dsn: str = os.getenv("POSTGRES_DSN", "postgresql://overnight:overnight@localhost:5432/overnight")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    langfuse_host: str = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
    # Model routing: cheap ADK glue vs. the subscription-backed Code Surgeon (set externally).
    reasoning_model: str = os.getenv("REASONING_MODEL", "anthropic/claude-haiku-4-5")
    surgeon_model: str = os.getenv("SURGEON_MODEL", "claude-opus-4-8")
    # Whether the Code Surgeon authenticates via Claude subscription (CLI/Agent SDK OAuth)
    # or a per-token API key. Subscription is flat-rate; API scales to many parallel agents.
    surgeon_use_subscription: bool = os.getenv("SURGEON_USE_SUBSCRIPTION", "true").lower() == "true"
    # Which forge the deterministic sweep executors target ('github' | 'gitlab').
    forge_provider: str = os.getenv("FORGE", "github")
    dry_run: bool = os.getenv("OVERNIGHT_DRY_RUN", "false").lower() == "true"


@dataclass
class Environment:
    settings: Settings = field(default_factory=Settings)
    secrets: dict[str, str] = field(default_factory=dict)

    def require(self, key: str) -> str:
        val = self.secrets.get(key) or os.getenv(key, "")
        if not val:
            raise RuntimeError(f"missing required secret/env: {key}")
        return val

    def get(self, key: str, default: str = "") -> str:
        return self.secrets.get(key) or os.getenv(key, default)


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _decrypt_sops(path: Path) -> dict[str, str]:
    """Decrypt a SOPS-encrypted dotenv/json in memory. No-op if sops/file absent."""
    if not path.exists() or not shutil.which("sops"):
        return {}
    try:
        raw = subprocess.run(
            ["sops", "-d", str(path)], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except json.JSONDecodeError:
            return {}
    # dotenv-style output
    tmp: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            tmp[k.strip()] = v.strip().strip('"').strip("'")
    return tmp


def _fetch_secrets(root: Path) -> dict[str, str]:
    """Tiered: encrypted SOPS first, then plain .env, then process env. Returns in-memory dict.

    Plan B swap point: replace this body with a Google Secret Manager lookup.
    """
    secrets: dict[str, str] = {}
    # Lowest precedence: plain .env (dev). Highest: SOPS, then explicit process env.
    secrets.update(_load_dotenv(root / ".env"))
    secrets.update(_decrypt_sops(root / ".env.sops"))
    for key in (*REQUIRED_SECRET_KEYS, *OPTIONAL_SECRET_KEYS):
        if os.getenv(key):
            secrets[key] = os.environ[key]
    return secrets


def init_environment(root: str | os.PathLike[str] | None = None) -> Environment:
    """Discover settings + load secrets in memory. Call once at process start."""
    base = Path(root) if root else Path.cwd()
    return Environment(settings=Settings(), secrets=_fetch_secrets(base))
