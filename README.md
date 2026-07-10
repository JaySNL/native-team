# native-team

Run a `claude` lead and interactive `qwen` grunts in adjacent tmux panes, coordinated by a
directory you can `cat`. No daemon, no Electron, no copy-paste.

**A grunt's answer is not trusted.** Measured citation accuracy on real tasks was 2/5, 0/4, 3/4 —
and every miss was caught only by re-reading the cited line. So `team verify` re-reads every line a
grunt cites, compares the evidence byte-for-byte, and **fails closed**. On the first task ever sent
to a live grunt it caught one: qwen cited `team/protocol.py:10` for a symbol that is on line 8.

That check is the reason this exists. The tmux plumbing is the easy part.

## Quickstart

    team init                                          # create .team/, install grunt qwen settings
    team-up 1                                          # tmux session: lead + 1 grunt
    team send grunt1 --question "Where is X defined?" --scope src/A.cs
    team wait --task 001 --timeout 600                 # background this from the lead
    team verify 001                                    # re-reads every cited line; exit 1 on any FAIL
    team down                                          # restore .qwen/settings.json, remove the bus

Install once:

    ln -sf "$PWD/bin/team" ~/.local/bin/team
    ln -sf "$PWD/bin/team-up" ~/.local/bin/team-up

`team` and `team-up` run from inside whatever repo they manage, not from here.

## How it works

The bus is a directory. An inbox file means pending; a result file means done. There is no status
field and no board file, so the board cannot drift out of sync with reality — it is `ls .team/inbox`.
(AionUi stores a `status` column some agent must remember to `UPDATE`; in its live database,
finished tasks still read `pending`.)

`team result done` **seals, then announces**: the result is renamed into `results/` before the
announcement is written to the lead's inbox, so a lead woken by an announcement always finds the
result already on disk.

`team wait` blocks, and its **exit code is the wake signal**. Background it from the lead.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 1 | `verify` found at least one FAIL (fails closed; `--lenient` forces 0) |
| 2 | target pane gone |
| 3 | refused — schema violation, invalid state, or a missing/unreadable file |
| 4 | timeout |

Only `team/panes.py` knows tmux exists. `wait.py` polls the filesystem rather than using
`tmux wait-for`, whose signals latch server-globally and produce stale false wakes (measured — see
`docs/tmux-capabilities.md`).

## Two things to know before you run it

- **`team init` changes your repo.** It writes `.qwen/settings.json`, which puts your own `qwen` in
  that repo into YOLO mode with no `CLAUDE.md` context until `team down` restores it. The `init`
  output says so.
- **The grunt's shell is unrestricted.** qwen ignores `coreTools` allowlists — a probe confirmed
  `echo SHELL_RAN` ran despite an allowlist scoped to `team` only. `excludeTools` removes the write
  tools, but a grunt can still mutate files via shell (`sed -i`). This is a recorded, accepted risk,
  not an oversight. See `docs/validation-phase1.md`.

## Docs

- Design: `docs/superpowers/specs/2026-07-10-native-team-design.md`
- Why not a tmux MCP server, `tmux wait-for`, or control mode: `docs/tmux-capabilities.md`
- What AionUi's daemon taught us: `docs/prior-art-aionui.md`
- Live validation results: `docs/validation-phase1.md`
- The nine papercuts this answers: `HANDOFF.md`

## Development

    python3 -m unittest discover -s tests -t .

200 tests, stdlib only, no pytest. The end-to-end test drives a real tmux session with a scripted
grunt, and is skipped only if tmux is absent.
