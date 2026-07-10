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
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

PALETTE = re.compile(r"\(\d+/\d+\)")

# "The TUI is drawn and will not drop a keystroke."
#
# NOT the input placeholder. `Type your message or @path/to/file` was the
# obvious choice and it is wrong: qwen ROTATES that placeholder through ghost
# suggestions. Measured live -- a pane sat showing `post comments`, and another
# showed `team result done --task 002`, which reads exactly like a half-typed
# command and is not: typing replaces it, backspace restores it, Enter submits
# nothing. Keying on it made `send` refuse a perfectly healthy grunt with "the
# agent may have failed to start".
#
# The mode footer (`YOLO mode (shift + tab to cycle)` / `⏸ Ask permissions
# (shift + tab to cycle)`) is drawn once the TUI is up and does not rotate. The
# placeholder is kept as an alternative so a qwen that drops the footer still
# matches something.
READY = re.compile(r"shift \+ tab to cycle|Type your message")

# qwen's spinner while a turn is in flight: "(7.5s . esc to cancel)".
BUSY = re.compile(r"esc to cancel")


class PaneError(Exception):
    pass


def write_death_hook(team_bin: Path, root: Path, agent: str,
                     dirname: str = "team-hooks-") -> Path:
    """Write the `pane-died` hook script and return its path.

    Never under `.team/`. `team down` deletes `.team` while panes may still be
    alive, and tmux would then fire a hook whose script has vanished.

    `mktemp` also sidesteps a hazard measured live against tmux 3.7b: a hook
    fired by tmux hands `run-shell`'s stored argument to `sh -c` unquoted, so a
    script path containing a space reliably fails -- even though the identical
    quoted path works when `tmux run-shell` is invoked directly. A mkdtemp path
    contains no metacharacters no matter where the repo lives.

    Every interpolated path is still `shlex.quote`d, because the script's own
    body is shell.
    """
    d = Path(tempfile.mkdtemp(prefix=dirname))
    script = d / f"{agent}-died.sh"
    q = shlex.quote
    script.write_text(
        "#!/bin/sh\n"
        f"exec {q(str(team_bin))} --root {q(str(root))} msg "
        f"--agent {q(agent)} --failed --task pane-died {q('grunt pane died')}\n"
    )
    script.chmod(0o755)
    return script


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError as exc:
        # tmux absent. A caller guarding with `except PaneError` should not
        # have to also guard for the binary being missing.
        raise PaneError(f"tmux not found on PATH: {argv[0]}") from exc


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
        """Is `target` a pane that can be sent to?

        Both halves are measured, and the obvious implementations are wrong:

        * `list-panes -t <pane>` lists every pane in that pane's *window*, so it
          answers "does this window have panes", not "does this pane exist". The
          old implementation asked exactly that, and said yes for a pane that
          was gone.
        * `display-message -p -t <bogus>` exits **0** with empty stdout. The
          return code cannot be the signal; empty output is.

        A pane whose process died with `remain-on-exit on` still exists, still
        has an id, and still accepts `send-keys` with rc 0 -- silently. To this
        module that is not a pane you can send to.
        """
        proc = self.runner(["tmux", "display-message", "-p", "-t", target,
                            "#{pane_id} #{pane_dead}"])
        out = proc.stdout.strip()
        if proc.returncode != 0 or not out:
            return False
        return out.split()[-1] == "0"

    def capture(self, target: str) -> str:
        return self._tmux("capture-pane", "-p", "-t", target).stdout

    def wait_ready(self, target: str, timeout: float = 60.0) -> None:
        """Block until the pane's TUI is drawn.

        A pane created at T+0 runs a qwen that takes ~6s to draw itself. Keys
        sent before then are dropped on the floor -- no error, no task, and a
        lead that blocks in `team wait` until its timeout. `team-up` never hit
        this because a human took seconds to attach before sending anything.
        Spawning grunts on demand removes the human.

        Drawn, deliberately -- not *idle*. `--supersede` exists to interrupt a
        working grunt, and `send_line`'s leading Escape is what cancels its
        turn. A readiness check that waited for the spinner to clear would make
        supersede wait for the very turn it is meant to kill. `BUSY` is exported
        for callers that genuinely want idleness; nothing needs it yet.
        """
        deadline = time.monotonic() + timeout
        while True:
            if READY.search(self.capture(target)):
                return
            if time.monotonic() >= deadline:
                raise PaneError(
                    f"{target}: no prompt after {timeout}s; the agent may have "
                    f"failed to start. Look at the pane."
                )
            self.sleep(0.25)

    def split(self, target: str, cwd: Path, command: str,
              env: dict[str, str] | None = None) -> str:
        """Split `target`, returning the NEW pane's id.

        The id, not the index. Indices renumber when a pane dies: kill index 1
        of three and index 2 becomes index 1, so a roster keyed on indices comes
        to name a different pane. Measured. A pane id never moves.

        `-e` sets environment for the new pane only (measured), which is how a
        grunt gets `team` on its PATH without exporting anything into the
        session it was launched from.
        """
        argv = ["tmux", "split-window", "-P", "-F", "#{pane_id}",
                "-t", target, "-c", str(cwd)]
        for key, value in sorted((env or {}).items()):
            argv += ["-e", f"{key}={value}"]
        argv.append(command)
        proc = self.runner(argv)
        pane = proc.stdout.strip()
        if proc.returncode != 0 or not pane:
            raise PaneError(f"split-window failed: {proc.stderr.strip() or 'no pane id'}")
        self._tmux("select-layout", "-t", target, "tiled")
        return pane

    def kill(self, target: str) -> None:
        """Kill a pane. A pane that is already gone is not an error -- a grunt
        the user closed by hand must not make `grunt rm` or `down` fail."""
        self.runner(["tmux", "kill-pane", "-t", target])

    def new_session(self, session: str, cwd: Path, command: str) -> str:
        argv = ["tmux", "new-session", "-d", "-s", session, "-c", str(cwd),
                "-P", "-F", "#{pane_id}", command]
        proc = self.runner(argv)
        pane = proc.stdout.strip()
        if proc.returncode != 0 or not pane:
            raise PaneError(f"new-session failed: {proc.stderr.strip() or 'no pane id'}")
        return pane

    def install_death_hook(self, target: str, script: Path) -> None:
        """Report a dead grunt to the lead the instant it dies.

        `remain-on-exit on` is what makes `pane-died` fire at all (measured: with
        it off the pane is destroyed and only `pane-exited` fires) and it keeps
        the corpse visible in the layout. It is also why `exists()` must check
        `pane_dead`.
        """
        self._tmux("set-option", "-p", "-t", target, "remain-on-exit", "on")
        self._tmux("set-hook", "-p", "-t", target, "pane-died",
                   f"run-shell {shlex.quote(str(script))}")

    def pipe_pane(self, target: str, logfile: Path) -> None:
        self._tmux("pipe-pane", "-o", "-t", target, f"cat >> {shlex.quote(str(logfile))}")

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
