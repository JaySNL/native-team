"""One private git worktree per grunt, under `.team/work/<agent>`.

A build task's safety check is "the grunt changed only what it declared".
In the tree `team up` actually creates -- one lead and two or three grunts
sharing a checkout -- that check is unsound: grunt2 creating a file, or the
lead editing one, fails grunt1's check. Containment needs a tree the grunt
owns.

Placed under `.team/` because `init` already gitignores it, so the main tree's
`git status` never sees the worktrees.

The grunt pane's cwd **is** its worktree (spec Amendment 1). It has to be: qwen
resolves its project root from cwd, and every file tool it owns -- `WriteFile`
above all -- resolves relative paths against that root and takes no cwd of its
own. A pane rooted in the main tree writes into the main tree, where the
containment check cannot see it. Measured, task 013.

Every subprocess call is an argv list run without a shell, and every one goes
through `runner` so tests can watch or fake it.
"""
import subprocess
from pathlib import Path

from team import bus

WORK = "work"

# Files this tool puts into a grunt's worktree itself. They are not the grunt's
# work: containment must not blame the grunt for them, `down` must not refuse
# teardown over them, and `collect` never copies them out. A grunt rewriting its
# own `.qwen/` is inside its own fence -- it changes nothing outside the
# worktree, so this exemption is not a hole in containment.
PROVISIONED = (".qwen/",)


def is_provisioned(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in PROVISIONED)


def porcelain_rel(line: str) -> str:
    """`?? sub/A.cs` / ` M a.txt` -> `sub/A.cs` / `a.txt`."""
    return line.split(maxsplit=1)[-1].strip('"')


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

    # --- repository setup, for `team bootstrap` -----------------------------
    # Not worktree operations, but the same subprocess seam, the same fake
    # runner in tests, and the same rule: argv lists, never a shell string.

    def toplevel(self, root: Path) -> Path | None:
        """The git root `root` belongs to, or None if it belongs to none.

        Load-bearing for `bootstrap`: a directory *inside* another repo has no
        `.git` of its own, so a naive check would `git init` a nested repo --
        and `bus_root()` would meanwhile walk up and find the parent's bus.
        """
        rc, out = self._run(["git", "rev-parse", "--show-toplevel"], root)
        return Path(out.strip()) if rc == 0 and out.strip() else None

    def init_repo(self, root: Path) -> None:
        self._git(root, "init", "-q", ".")

    def empty_commit(self, root: Path, message: str) -> None:
        self._git(root, "commit", "-q", "--allow-empty", "-m", message)

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
        """Porcelain lines for anything modified or untracked in the worktree,
        minus the files this tool provisioned there.

        `-uall` is not optional: plain `--porcelain` collapses a new untracked
        directory to a single `?? sub/` entry, so a file written inside one is
        invisible. The whole point of this call is to notice files.

        Filtering `PROVISIONED` here rather than at each call site keeps the
        containment baseline and the containment check reading the same list.
        """
        return [line for line in
                self._git(path(root, agent), "status", "--porcelain", "-uall")
                .splitlines()
                if line.strip() and not is_provisioned(porcelain_rel(line))]

    def main_dirty(self, root: Path, paths: list[str]) -> list[str]:
        """Porcelain lines for `paths` in the MAIN tree.

        A grunt reads a detached checkout of HEAD, while `verify` resolves its
        citations against the main tree. If a scope path differs between the
        two, the grunt cites the file it read and `verify` calls it fabricated.
        `send` asks this first and refuses rather than dispatch a stale read.
        """
        if not paths:
            return []
        return [line for line in
                self._git(root, "status", "--porcelain", "-uall", "--", *paths)
                .splitlines() if line.strip()]

    def is_ignored(self, root: Path, agent: str, rel: str) -> bool:
        """Is `rel` gitignored *in the worktree*?

        Asked in the worktree, not the main tree: a worktree is checked out
        from HEAD, so its `.gitignore` is the committed one and may differ from
        the uncommitted file the lead is looking at.
        """
        rc, _ = self._run(["git", "check-ignore", "-q", rel], path(root, agent))
        return rc == 0

    def build(self, root: Path, agent: str, build_dir: str,
              argv: list[str]) -> tuple[int, str]:
        """Run the build command inside the worktree. argv, never a shell
        string: the command round-trips through the bus, which a grunt's
        unrestricted shell can rewrite."""
        cwd = path(root, agent) / build_dir
        if not cwd.is_dir():
            raise WorktreeError(f"build dir does not exist in worktree: {build_dir}")
        return self._run(argv, cwd)

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
