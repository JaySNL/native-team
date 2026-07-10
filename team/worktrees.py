"""One private git worktree per grunt, under `.team/work/<agent>`.

A build task's safety check is "the grunt changed only what it declared".
In the tree `team up` actually creates -- one lead and two or three grunts
sharing a checkout -- that check is unsound: grunt2 creating a file, or the
lead editing one, fails grunt1's check. Containment needs a tree the grunt
owns.

Placed under `.team/` because `init` already gitignores it, so the main tree's
`git status` never sees the worktrees. The panes' cwd stays the main root --
see the worktree spec -- and only a build task's shell commands `cd` in here.

Every subprocess call is an argv list run without a shell, and every one goes
through `runner` so tests can watch or fake it.
"""
import subprocess
from pathlib import Path

from team import bus

WORK = "work"


class WorktreeError(Exception):
    """A git worktree operation failed, or refused to run."""


def work_dir(root: Path) -> Path:
    return bus.team_dir(root) / WORK


def path(root: Path, agent: str) -> Path:
    return work_dir(root) / agent


def default_runner(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Run argv in cwd. Returns (returncode, stdout+stderr)."""
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    except (OSError, FileNotFoundError) as exc:
        raise WorktreeError(f"could not run {argv[0]!r}: {exc}") from exc
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class Worktrees:
    def __init__(self, runner=default_runner):
        self._run = runner

    def _git(self, cwd: Path, *args: str) -> str:
        rc, out = self._run(["git", *args], cwd)
        if rc != 0:
            raise WorktreeError(f"git {' '.join(args)} failed: {out.strip()}")
        return out

    def has_commit(self, root: Path) -> bool:
        rc, _ = self._run(["git", "rev-parse", "--verify", "HEAD"], root)
        return rc == 0

    def add(self, root: Path, agent: str) -> Path:
        """Create `<root>/.team/work/<agent>` detached at HEAD."""
        if not self.has_commit(root):
            raise WorktreeError(
                f"cannot create a worktree for {agent!r}: this repository has no "
                f"commits, so there is nothing to check out"
            )
        target = path(root, agent)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._git(root, "worktree", "add", "--detach", "-q", str(target), "HEAD")
        return target

    def dirty(self, root: Path, agent: str) -> list[str]:
        """Porcelain lines for anything modified or untracked in the worktree.

        `-uall` is not optional: plain `--porcelain` collapses a new untracked
        directory to a single `?? sub/` entry, so a file written inside one is
        invisible. The whole point of this call is to notice files.
        """
        return [line for line in
                self._git(path(root, agent), "status", "--porcelain", "-uall")
                .splitlines() if line.strip()]

    def remove(self, root: Path, agent: str) -> None:
        self._git(root, "worktree", "remove", "--force", str(path(root, agent)))

    def prune(self, root: Path) -> None:
        self._git(root, "worktree", "prune")

    def agents(self, root: Path) -> list[str]:
        """Agents that currently have a *worktree*, sorted.

        The `.git` check is not decoration. A plain directory left under
        `work/` is not a worktree, and `git status` run inside one resolves to
        the enclosing repo and reports the whole main tree as dirty -- which
        would make `team down` refuse forever, blaming a grunt that never ran.
        """
        wd = work_dir(root)
        if not wd.is_dir():
            return []
        return sorted(p.name for p in wd.iterdir()
                      if p.is_dir() and (p / ".git").exists())
