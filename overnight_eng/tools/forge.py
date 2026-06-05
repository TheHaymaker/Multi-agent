"""Forge abstraction — one semantic API over GitHub and GitLab.

The specialists speak in neutral terms (file an issue, open a *draft PR*, comment, rebase a
branch). This module maps those to each provider's actual MCP tool calls, papering over the
differences so GitLab is a first-class peer of GitHub:

  * GitHub: pull requests, ``draft: true``; review comments on "pull request".
  * GitLab: merge requests, draft signalled by a ``Draft:`` title prefix; comments on the MR.

The actual MCP tool invocation is injected (``invoke``) so the mapping is unit-tested without
a live forge or the ADK runtime. ``build_forge`` picks the right implementation; the sweep
paths use :func:`make_pr_executor` / :func:`make_issue_executor` to get gated executors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

# A thin seam: given a tool name + kwargs, perform the MCP call and return a human ref (url/id).
Invoke = Callable[[str, dict[str, Any]], str]


class Forge(Protocol):
    name: str

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str: ...
    def open_draft_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str: ...
    def update_pr(self, repo: str, number: int, body: str) -> str: ...
    def comment(self, repo: str, number: int, body: str) -> str: ...
    def rebase_branch(self, repo: str, branch: str, base: str) -> str: ...


@dataclass
class GitHubForge:
    invoke: Invoke
    name: str = "github"

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        return self.invoke("create_issue", {"repo": repo, "title": title, "body": body,
                                            "labels": labels})

    def open_draft_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        return self.invoke("create_pull_request", {"repo": repo, "head": head, "base": base,
                                                   "title": title, "body": body, "draft": True})

    def update_pr(self, repo: str, number: int, body: str) -> str:
        return self.invoke("update_pull_request", {"repo": repo, "pullNumber": number, "body": body})

    def comment(self, repo: str, number: int, body: str) -> str:
        return self.invoke("add_issue_comment", {"repo": repo, "issueNumber": number, "body": body})

    def rebase_branch(self, repo: str, branch: str, base: str) -> str:
        # GitHub's "update branch" merges/rebases the base into the PR head branch.
        return self.invoke("update_pull_request_branch", {"repo": repo, "branch": branch, "base": base})


@dataclass
class GitLabForge:
    invoke: Invoke
    name: str = "gitlab"

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        return self.invoke("create_issue", {"project": repo, "title": title, "description": body,
                                            "labels": ",".join(labels)})

    def open_draft_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        # GitLab merge requests; draft is signalled by a "Draft:" title prefix.
        draft_title = title if title.startswith("Draft:") else f"Draft: {title}"
        return self.invoke("create_merge_request", {"project": repo, "source_branch": head,
                                                    "target_branch": base, "title": draft_title,
                                                    "description": body})

    def update_pr(self, repo: str, number: int, body: str) -> str:
        return self.invoke("update_merge_request", {"project": repo, "merge_request_iid": number,
                                                    "description": body})

    def comment(self, repo: str, number: int, body: str) -> str:
        return self.invoke("create_merge_request_note", {"project": repo, "merge_request_iid": number,
                                                         "body": body})

    def rebase_branch(self, repo: str, branch: str, base: str) -> str:
        # GitLab has a native rebase endpoint for the MR's source branch.
        return self.invoke("rebase_merge_request", {"project": repo, "source_branch": branch})


def build_forge(provider: str, invoke: Invoke) -> Forge:
    """Return the forge implementation for ``provider`` ('github' | 'gitlab')."""
    if provider == "github":
        return GitHubForge(invoke=invoke)
    if provider == "gitlab":
        return GitLabForge(invoke=invoke)
    raise ValueError(f"unknown forge provider '{provider}'")


# --- executors used by the sweeps (close over the forge; gated by PolicyGuard) -------


def make_pr_executor(forge: Forge) -> Callable[[Any], str]:
    """Executor for OPEN_DRAFT_PR: takes a ChangeProposal, opens a draft PR/MR."""

    def execute(proposal: Any) -> str:
        return forge.open_draft_pr(
            repo=getattr(proposal, "repo", "") or "",
            head=proposal.branch,
            base=proposal.base_branch,
            title=proposal.title,
            body=proposal.body,
        )

    return execute


def make_issue_executor(forge: Forge) -> Callable[[Any], str]:
    """Executor for CREATE_ISSUE: takes an ActionRequest, files an issue."""

    def execute(req: Any) -> str:
        payload = getattr(req, "payload", {}) or {}
        return forge.create_issue(
            repo=getattr(req, "repo", "") or "",
            title=req.summary,
            body=payload.get("body", req.summary),
            labels=payload.get("labels", []),
        )

    return execute


def make_pr_action_executor(forge: Forge) -> Callable[[Any], str]:
    """Executor for PR-coordination ActionRequests (rebase / comment / update)."""

    def execute(req: Any) -> str:
        from overnight_eng.models import ActionType

        payload = getattr(req, "payload", {}) or {}
        repo = getattr(req, "repo", "") or ""
        number = int(payload.get("pr", 0) or 0)
        if req.action is ActionType.REBASE_AGENT_BRANCH:
            return forge.rebase_branch(repo, req.branch or "", req.target_branch or "")
        if req.action is ActionType.UPDATE_DRAFT_PR:
            return forge.update_pr(repo, number, payload.get("body", req.summary))
        if req.action is ActionType.COMMENT:
            return forge.comment(repo, number, req.summary)
        return f"{req.action.value}: {req.summary}"

    return execute
