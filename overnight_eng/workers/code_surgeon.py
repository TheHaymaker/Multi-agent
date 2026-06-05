"""Code Surgeon — the shared code-editing worker used by the code-touching agents.

This is the one place real file edits happen. It runs the **Claude Agent SDK** (which can
authenticate headless on a Claude Pro/Max **subscription** — flat-rate — or via an API key),
so the expensive multi-file editing work doesn't go through ADK's per-token glue. The Agent
SDK already provides the agent loop, tool-permission gating, and bash/git access, so it *is*
most of our PolicyGuard substrate.

Safety model:
  * Every task runs in a throwaway **git worktree** on an agent-owned branch, so edits can't
    touch the developer's working tree or a protected branch.
  * The worker NEVER opens the PR itself — it hands a :class:`ChangeProposal` back, and the
    orchestrator routes the ``OPEN_DRAFT_PR`` action through :class:`PolicyGuard`.
  * Small-PR discipline (e.g. for the TypeScript type-hygiene agent) is enforced via a
    line-count cap checked after the edit; oversized diffs are rejected and re-scoped.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from overnight_eng.app_utils.env import Environment


@dataclass
class TaskSpec:
    """A unit of code work handed to the surgeon by a specialist agent."""

    repo_path: str
    base_branch: str            # protected base to branch from / target the draft PR at
    title: str
    instructions: str           # what to change and why (from the specialist's finding)
    run_checks: list[str] = field(default_factory=list)  # e.g. ["npm test", "tsc --noEmit"]
    max_changed_lines: int = 150  # small-PR cap; keeps review easy + blast radius low
    branch_prefix: str = "agent"


@dataclass
class ChangeProposal:
    """Result of a surgery run — fed to the orchestrator to gate the draft PR."""

    branch: str
    base_branch: str
    title: str
    body: str
    changed_lines: int
    checks_passed: bool
    rejected_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected_reason and self.checks_passed


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _changed_line_count(worktree: str | Path, base: str) -> int:
    diff = _git(["diff", "--numstat", base], worktree).stdout
    total = 0
    for line in diff.splitlines():
        added, removed, *_ = (line.split("\t") + ["0", "0"])[:3]
        total += sum(int(x) for x in (added, removed) if x.isdigit())
    return total


class CodeSurgeon:
    """Runs a :class:`TaskSpec` in an isolated worktree via the Claude Agent SDK."""

    def __init__(self, env: "Environment") -> None:
        self.env = env

    async def run(self, spec: TaskSpec) -> ChangeProposal:
        branch = f"{spec.branch_prefix}/{_slug(spec.title)}-{uuid.uuid4().hex[:6]}"
        worktree = Path(spec.repo_path).parent / f".overnight-wt-{uuid.uuid4().hex[:8]}"

        # Isolated worktree on a fresh agent-owned branch off the protected base.
        _git(["worktree", "add", "-b", branch, str(worktree), spec.base_branch], spec.repo_path)
        try:
            await self._apply_edits(worktree, spec)
            checks_passed = self._run_checks(worktree, spec.run_checks)
            changed = _changed_line_count(worktree, spec.base_branch)

            rejected = ""
            if changed == 0:
                rejected = "no changes produced"
            elif changed > spec.max_changed_lines:
                rejected = (
                    f"diff too large ({changed} > {spec.max_changed_lines} lines) — "
                    f"re-scope into smaller PRs"
                )

            if not rejected:
                _git(["push", "-u", "origin", branch], worktree)

            return ChangeProposal(
                branch=branch,
                base_branch=spec.base_branch,
                title=spec.title,
                body=self._render_body(spec, changed, checks_passed),
                changed_lines=changed,
                checks_passed=checks_passed,
                rejected_reason=rejected,
            )
        finally:
            _git(["worktree", "remove", "--force", str(worktree)], spec.repo_path)

    async def _apply_edits(self, worktree: Path, spec: TaskSpec) -> None:
        """Drive the Claude Agent SDK to make the edits inside ``worktree``."""
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
        except Exception:
            # SDK unavailable (e.g. tests/dev without it). The worktree is set up; a real run
            # requires the SDK + subscription/API auth. Leave the tree untouched.
            return

        options = ClaudeAgentOptions(
            cwd=str(worktree),
            model=self.env.settings.surgeon_model,
            # Propose-only: allow edits + local checks, deny network-y / destructive git ops.
            allowed_tools=["Read", "Edit", "Write", "Bash(npm test:*)", "Bash(tsc:*)", "Bash(git diff:*)"],
            permission_mode="acceptEdits",
        )
        prompt = (
            f"{spec.instructions}\n\n"
            f"Constraints: keep the change under {spec.max_changed_lines} changed lines; "
            f"make the smallest correct edit; run the project's type-check/tests before finishing."
        )
        async for _ in query(prompt=prompt, options=options):  # consume the agent loop
            pass

    def _run_checks(self, worktree: Path, checks: list[str]) -> bool:
        for cmd in checks:
            res = subprocess.run(cmd, cwd=str(worktree), shell=True, capture_output=True, text=True)
            if res.returncode != 0:
                return False
        return True

    def _render_body(self, spec: TaskSpec, changed: int, checks_passed: bool) -> str:
        return (
            f"**Automated proposal (draft, propose-only)**\n\n{spec.instructions}\n\n"
            f"- changed lines: {changed} (cap {spec.max_changed_lines})\n"
            f"- checks: {'passed ✅' if checks_passed else 'FAILED ❌'} ({', '.join(spec.run_checks) or 'none'})\n\n"
            f"Generated overnight. Review before merge."
        )


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(filter(None, keep.split("-")))[:40]
