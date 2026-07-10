"""The only module in this project permitted to know that tmux exists.

Every other module (bus.py, schema.py, verify.py, log.py, wait.py, ...) must keep
working if tmux were swapped for zellij tomorrow. `wait.py` polls the filesystem
rather than calling `tmux wait-for` specifically to preserve that seam -- this
module is where tmux-specific knowledge is allowed to live, and nowhere else.

Measured facts this implementation encodes (probed live against a running qwen
pane; see docs/tmux-capabilities.md and docs/prior-art-aionui.md):

1. `send-keys -l TEXT` delivers exact literal text into qwen's Ink TUI only when
   issued as a *separate* tmux invocation from `send-keys Enter`. A single
   combined call is unreliable. Every send is a bare argv list handed straight
   to `subprocess` -- no shell string is ever built, so there is nothing for a
   task body to break out of.

2. Typing a leading `/` (as `/clear` does) opens qwen's 70-entry command
   palette, and a bare `Enter` afterwards selects whichever completion happens
   to be highlighted -- usually not the command that was typed. So every send
   dismisses palette state with a leading `Escape` first, and `clear_context`
   does not just fire `/clear` and hope: it re-captures the pane and waits for
   the palette glyph `(n/70)` to disappear before returning.

3. That same leading `Escape` ALSO cancels an in-flight qwen turn (qwen's own
   spinner literally reads "(7.5s . esc to cancel)"). This is not an
   accident to route around -- it is load-bearing in two places outside this
   module:

     * `--supersede` genuinely halts the superseded grunt's work, not merely
       its late result. Superseding a task calls `clear_context`, whose leading
       `Escape` cancels the grunt's running turn before its context is wiped.
     * `--reply` is only ever allowed to fire against an agent whose last
       message was `blocked`, i.e. an agent idle at its prompt. That guard is
       load-bearing *because* `--reply` also sends `Escape` first: if it were
       ever loosened to permit replying to a busy agent, the reply would
       silently cancel that agent's in-flight turn instead of queuing behind
       it.

   Both callers live outside this module (the reply/supersede commands); this
   module only supplies the `Escape`-first mechanism and must never drop it.
"""
import re
import shlex
import subprocess
import time
from pathlib import Path

PALETTE = re.compile(r"\(\d+/\d+\)")


class PaneError(Exception):
    pass


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


class Panes:
    def __init__(self, runner=default_runner, sleep=time.sleep):
        self.runner = runner
        self.sleep = sleep

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        proc = self.runner(["tmux", *args])
        if proc.returncode != 0:
            raise PaneError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def exists(self, target: str) -> bool:
        session = target.split(":", 1)[0]
        if self.runner(["tmux", "has-session", "-t", session]).returncode != 0:
            return False
        proc = self.runner(["tmux", "list-panes", "-t", target, "-F", "#{pane_id}"])
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def capture(self, target: str) -> str:
        return self._tmux("capture-pane", "-p", "-t", target).stdout

    def send_line(self, target: str, text: str) -> None:
        # Escape first: dismisses any open command palette AND cancels an
        # in-flight qwen turn. Both effects are load-bearing elsewhere (see
        # module docstring) -- do not remove or reorder this call.
        self._tmux("send-keys", "-t", target, "Escape")
        self.sleep(0.15)
        self._tmux("send-keys", "-t", target, "-l", text)
        self.sleep(0.15)
        self._tmux("send-keys", "-t", target, "Enter")

    def clear_context(self, target: str, timeout: float = 10.0) -> None:
        self.send_line(target, "/clear")
        deadline = time.monotonic() + timeout
        while True:
            if not PALETTE.search(self.capture(target)):
                return
            if time.monotonic() >= deadline:
                raise PaneError(
                    f"{target}: command palette still open after /clear; "
                    f"pane may have selected the wrong command"
                )
            self.sleep(0.25)

    def pipe_pane(self, target: str, logfile: Path) -> None:
        self._tmux("pipe-pane", "-o", "-t", target, f"cat >> {shlex.quote(str(logfile))}")
