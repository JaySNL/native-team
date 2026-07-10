# Phase-1 validation results

| # | Question | Verdict | Evidence |
|---|---|---|---|
| A | Background Bash re-invokes an idle lead | PASS | controller observed run_in_background task-notification re-invoking an idle session, 2026-07-10 |
| B | qwen honours `coreTools: ["run_shell_command(team)"]` | FAIL | `echo SHELL_RAN` was executed — pane showed `✓ Shell echo SHELL_RAN ... SHELL_RAN` despite `coreTools` scoping the allowlist to `run_shell_command(team)` only |

B failed, so `run_shell_command` stays unrestricted. Read-only is enforced by
`excludeTools` alone, and a grunt can still mutate files via shell (e.g. `sed -i`).
This is an accepted, recorded risk — see the spec's "Still unverified" section.

| C | A real qwen grunt round-trips a task through the bus | PASS | live 2026-07-10: `team send` → grunt ran `team result add`/`done` → backgrounded `team wait --for lead` exited 0 |
| D | Verification catches a real fabrication | PASS | live: qwen cited `team/protocol.py:10` for `TEMPLATE`, which is on line 8 → `FABRICATED` |
| E | `pane-died` hook reports a dead grunt | PASS | live: killed the pane's process with `remain-on-exit on`; `failed` message reached the lead's inbox in ~3s |

D is the project's reason to exist, reproduced against a real model on the first
task ever sent. It is also why `team verify` now fails closed: at the time of the
live run it printed `FABRICATED` and exited `0`.
