"""MCP toolset wiring — the capability layer, identical in spirit to Dev Signal's mcp_config.

Every external integration is an MCP server wrapped by ADK's ``McpToolset``; agents receive a
standardized tool surface instead of bespoke API glue. Servers used here all exist today:

  * **Sentry**   — hosted MCP at ``https://mcp.sentry.dev/mcp`` (issue/event triage)
  * **Grafana**  — ``mcp-grafana`` server (stdio) for dashboards/alerts/metrics
  * **GitHub**   — official GitHub MCP server (HTTP)
  * **GitLab**   — official GitLab MCP server (HTTP)
  * **Jira**     — Atlassian MCP server (stdio/HTTP) for backlog polling

ADK is imported lazily inside each factory so this module (and the test suite) import cleanly
without the agent runtime installed. Credentials come from the in-memory secret dict, never
from process env at rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment


def _http_toolset(url: str, headers: dict[str, str], timeout: float = 120.0) -> Any:
    """Build an McpToolset over Streamable HTTP."""
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, headers=headers, timeout=timeout)
    )


def _stdio_toolset(command: str, args: list[str], env: dict[str, str], timeout: float = 120.0) -> Any:
    """Build an McpToolset over a local stdio subprocess."""
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command=command, args=args, env=env),
            timeout=timeout,
        )
    )


# --- one factory per integration; each returns an McpToolset -----------------------


def sentry_toolset(env: "Environment") -> Any:
    token = env.require("SENTRY_AUTH_TOKEN")
    return _http_toolset("https://mcp.sentry.dev/mcp", {"Authorization": f"Bearer {token}"})


def grafana_toolset(env: "Environment") -> Any:
    return _stdio_toolset(
        "mcp-grafana",
        [],
        {
            "GRAFANA_URL": env.get("GRAFANA_URL", "http://localhost:3001"),
            "GRAFANA_API_KEY": env.get("GRAFANA_TOKEN"),
        },
    )


def github_toolset(env: "Environment") -> Any:
    token = env.require("GITHUB_TOKEN")
    return _http_toolset(
        "https://api.githubcopilot.com/mcp/", {"Authorization": f"Bearer {token}"}
    )


def gitlab_toolset(env: "Environment") -> Any:
    token = env.require("GITLAB_TOKEN")
    base = env.get("GITLAB_MCP_URL", "https://gitlab.com")
    return _http_toolset(
        f"{base.rstrip('/')}/api/v4/mcp", {"Authorization": f"Bearer {token}"}
    )


def jira_toolset(env: "Environment") -> Any:
    return _stdio_toolset(
        "npx",
        ["-y", "@aashari/mcp-server-atlassian-jira"],
        {
            "ATLASSIAN_SITE_NAME": env.get("JIRA_SITE", ""),
            "ATLASSIAN_USER_EMAIL": env.get("JIRA_EMAIL", ""),
            "ATLASSIAN_API_TOKEN": env.get("JIRA_API_TOKEN", ""),
        },
    )


# Registry so agents/scheduler can request toolsets by name and we can health-check them.
TOOLSET_FACTORIES = {
    "sentry": sentry_toolset,
    "grafana": grafana_toolset,
    "github": github_toolset,
    "gitlab": gitlab_toolset,
    "jira": jira_toolset,
}


def build_toolsets(env: "Environment", names: list[str]) -> dict[str, Any]:
    """Instantiate the named toolsets. Skips any whose required secret is missing."""
    out: dict[str, Any] = {}
    for name in names:
        factory = TOOLSET_FACTORIES.get(name)
        if factory is None:
            raise KeyError(f"unknown toolset '{name}'")
        try:
            out[name] = factory(env)
        except RuntimeError:
            # Missing credential -> skip, surfaced by the health check rather than crashing.
            continue
    return out
