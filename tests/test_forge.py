"""Tests for the GitHub/GitLab forge abstraction — proves parity of the mapping."""

from __future__ import annotations

from dataclasses import dataclass

from overnight_eng.models import ActionRequest, ActionType
from overnight_eng.tools.forge import (
    build_forge,
    make_issue_executor,
    make_pr_action_executor,
    make_pr_executor,
)


@dataclass
class _Recorder:
    calls: list[tuple[str, dict]]

    def __call__(self, tool: str, kwargs: dict) -> str:
        self.calls.append((tool, kwargs))
        return f"{tool}:ok"

    @property
    def last(self) -> tuple[str, dict]:
        return self.calls[-1]


def _rec() -> _Recorder:
    return _Recorder(calls=[])


# ----- issue parity ----------------------------------------------------------------


def test_github_create_issue() -> None:
    rec = _rec()
    build_forge("github", rec).create_issue("o/r", "boom", "details", ["bug"])
    tool, kw = rec.last
    assert tool == "create_issue" and kw["body"] == "details" and kw["labels"] == ["bug"]


def test_gitlab_create_issue_uses_description_and_joined_labels() -> None:
    rec = _rec()
    build_forge("gitlab", rec).create_issue("group/proj", "boom", "details", ["bug", "p1"])
    tool, kw = rec.last
    assert tool == "create_issue"
    assert kw["description"] == "details"        # GitLab uses 'description', not 'body'
    assert kw["labels"] == "bug,p1"              # comma-joined


# ----- draft PR vs draft MR --------------------------------------------------------


def test_github_open_draft_pr_sets_draft_true() -> None:
    rec = _rec()
    build_forge("github", rec).open_draft_pr("o/r", "agent/x", "main", "Fix types", "body")
    tool, kw = rec.last
    assert tool == "create_pull_request" and kw["draft"] is True
    assert kw["head"] == "agent/x" and kw["base"] == "main"


def test_gitlab_open_draft_mr_prefixes_title() -> None:
    rec = _rec()
    build_forge("gitlab", rec).open_draft_pr("group/proj", "agent/x", "main", "Fix types", "body")
    tool, kw = rec.last
    assert tool == "create_merge_request"
    assert kw["title"].startswith("Draft: ")            # GitLab draft convention
    assert kw["source_branch"] == "agent/x" and kw["target_branch"] == "main"


def test_gitlab_does_not_double_prefix_draft() -> None:
    rec = _rec()
    build_forge("gitlab", rec).open_draft_pr("p", "h", "main", "Draft: already", "b")
    assert rec.last[1]["title"] == "Draft: already"


# ----- rebase parity ---------------------------------------------------------------


def test_github_rebase_uses_update_branch() -> None:
    rec = _rec()
    build_forge("github", rec).rebase_branch("o/r", "agent/x", "main")
    assert rec.last[0] == "update_pull_request_branch"


def test_gitlab_rebase_uses_native_endpoint() -> None:
    rec = _rec()
    build_forge("gitlab", rec).rebase_branch("p", "agent/x", "main")
    assert rec.last[0] == "rebase_merge_request"


# ----- executors -------------------------------------------------------------------


def test_pr_executor_opens_draft_from_proposal() -> None:
    rec = _rec()
    from overnight_eng.workers.code_surgeon import ChangeProposal

    proposal = ChangeProposal("agent/x", "main", "T", "body", 10, True)
    proposal.repo = "o/r"  # type: ignore[attr-defined]
    make_pr_executor(build_forge("github", rec))(proposal)
    assert rec.last[0] == "create_pull_request"


def test_pr_action_executor_routes_by_action() -> None:
    rec = _rec()
    forge = build_forge("gitlab", rec)
    ex = make_pr_action_executor(forge)
    ex(ActionRequest(ActionType.REBASE_AGENT_BRANCH, actor="pr", summary="rebase",
                     branch="agent/x", target_branch="main", repo="p", payload={"pr": 5}))
    assert rec.last[0] == "rebase_merge_request"
    ex(ActionRequest(ActionType.COMMENT, actor="pr", summary="ack", repo="p", payload={"pr": 5}))
    assert rec.last[0] == "create_merge_request_note"


def test_unknown_provider_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_forge("bitbucket", _rec())
