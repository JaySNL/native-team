# tmux capabilities: what we use, what we rejected, and why

Measured against tmux 3.7b on 2026-07-10, prompted by "doesn't tmux have plugins / remote control?"

## TPM plugins — wrong layer

The tmux plugin ecosystem (TPM) is shell scripts that bind keys, paint the status bar, and persist
sessions. There is no message-passing or agent-control API. Not applicable.

## Control mode (`tmux -C`) — real, and rejected

`tmux -C attach` speaks a line-based protocol. Verified working:

```
%begin 1783642499 291 0
%end 1783642499 291 0
%session-changed $0 wf
```

It emits `%output %pane-id <bytes>` for live pane data, which could replace `pipe-pane`.

**Rejected.** Two reasons:

1. The `%output` payload is the same ANSI byte stream `pipe-pane` gives us — measured at 341 KB per
   four qwen turns, ~96% spinner redraw. Control mode does not make the transcript readable; only
   `team log` does.
2. It requires a **persistent client process** holding the connection open and parsing the stream.
   That is a daemon. `HANDOFF.md` chose the file bus specifically to avoid one, and the prior-art
   study (`docs/prior-art-aionui.md`) shows what a daemon costs: 13 delivery states, none of which
   answer whether a message arrived.

## `tmux wait-for` — tempting, rejected

`wait-for` is a real IPC primitive: `tmux wait-for -S <channel>` signals, `tmux wait-for <channel>`
blocks. It could replace `team wait`'s 250 ms poll loop.

**Rejected**, on measured behavior:

```
tmux wait-for -S chan1      # signal with NO waiter
tmux wait-for chan1         # -> returns immediately   (the signal LATCHED)
tmux wait-for chan1         # -> blocks                (latch was one-shot)
```

- **Signals latch.** A signal raised with nobody waiting is remembered. Channels are global to the
  tmux **server**, not scoped to a session. A stray signal from a previous or crashed team session
  makes the next `team wait` return instantly on nothing — the same stale-state hazard that
  `team init --force` exists to prevent, except invisible and with no file to inspect.
- **It couples the bus to tmux.** `panes.py` is the only module that knows tmux exists; that is what
  lets the bus survive a swap to zellij or to no multiplexer. `wait-for` would put tmux inside
  `wait.py`.
- **It depends on the grunt doing what it was told.** The grunt would have to call
  `tmux wait-for -S`. Papercut #3 is precisely a grunt not calling the thing it was told to call.
  When it fails to write a result file we observe a `TIMEOUT`. When it fails to signal a channel we
  observe an indistinguishable hang.

Polling costs nothing: turns take tens of seconds; the loop sleeps 250 ms.

## tmux MCP servers — real, and rejected

Asked 2026-07-10: "an MCP exists, aren't we making this hard?" Three were checked.

- **`nickgnd/tmux-mcp`** (TypeScript): `execute-command` + `get-command-result`, needs `--shell-type` to
  read exit status. Shell-shaped; no raw keystrokes. Our panes run qwen's Ink TUI — no shell prompt,
  no exit code. Does not fit.
- **`tmux-mcp-rs`** (Rust): genuinely capable. 50+ tools, `send-keys` with `literal`, `send-escape`,
  `send-cancel`, `capture-pane`, START/DONE marker tracking with real exit codes, allowlist policy on
  `execute-command`. Well built.
- `bnomei/tmux-mcp`: could not be verified; the URL served `tmux-mcp-rs` content.

**Rejected**, on scope rather than quality:

1. **It solves a problem we do not have.** Of eleven phase-1 modules, two touch tmux (`panes.py`,
   `bin/team-up`), and `panes.py` is ~80 lines of `subprocess.run(["tmux", ...])`. The nine papercuts
   in `HANDOFF.md` are grunts that cannot report back (#3), grunt output that is untrustworthy (#7),
   scarce lead context (#8), opaque delivery (#4), a board that never updates (#5). No tmux MCP
   touches any of them. The lead has never once struggled to drive tmux.
2. **It works against papercut #8.** 50+ tool schemas enter the lead's context on every turn. The
   papercut *is* the lead's context reaching 193k. `tmux send-keys` over Bash costs zero schema.
3. **It re-opens papercut #2.** That papercut was an MCP naming surface: qwen resolved
   `mcp__aionui-team__team_send_message` but not `team_send_message`, and concluded it had no team
   tools at all. A CLI on `$PATH` has no naming surface to get wrong.
4. **It is a resident third-party binary with write access to every pane** in the tmux server,
   including the lead's. That is the daemon the file bus exists to avoid.

Its best feature — marker-based `execute-command` tracking — is inert here: markers need a shell to
echo them. We would use `send-keys`, `send-escape`, `capture-pane`: three tools of fifty, each a
one-line subprocess call today. And we would lose what `panes.py` is *for* — that Escape must precede
every line (dismissing the command palette, *and* cancelling an in-flight turn), and that `/clear`
needs a postcondition check. tmux-mcp-rs does not know qwen exists.

**This decision is cheap to reverse.** `panes.py` is the only module permitted to know tmux exists, so
swapping its guts touches one file. It is a Task-6 implementation detail, not architecture.

The inverse trade is the one worth making, in phase 2: wrap the finished `team` CLI *as* an MCP
server, giving the lead `team_send` / `team_wait` / `team_verify` as first-class tools. That is ~40
lines, and it is worth doing only after the CLI has earned its semantics.

## Hooks — adopted

`set-hook -t <pane> pane-died '<command>'` is accepted by tmux 3.7b, and `run-shell` is a valid hook
command. Real events include `pane-died`, `pane-exited`, `client-attached`, `session-closed`.

**Adopted for one concrete win.** Today, a grunt whose pane dies is only detected when `team wait`
times out. With a hook set at pane creation, the death writes a `failed` message into the lead's
inbox immediately:

    tmux set-hook -t <pane> pane-died \
      'run-shell "team msg --agent <name> --failed --task pane-died \"grunt pane died\""'

This improves papercut #4 (delivery must be observable) without weakening anything: the hook's
effect is a **file**, so the bus is still a directory you can `cat`, and `wait.py` still knows
nothing about tmux.

Belongs in `bin/team-up` alongside the existing `pipe-pane` call. Phase 1, one line per pane.

Caveat to verify when implementing: `pane-died` fires only when `remain-on-exit` is on; otherwise the
pane is destroyed and `pane-exited` is the event. Test both before relying on either.
