# Phase-1 validation results

| # | Question | Verdict | Evidence |
|---|---|---|---|
| A | Background Bash re-invokes an idle lead | PASS | controller observed run_in_background task-notification re-invoking an idle session, 2026-07-10 |
| B | qwen honours `coreTools: ["run_shell_command(team)"]` | FAIL | `echo SHELL_RAN` was executed — pane showed `✓ Shell echo SHELL_RAN ... SHELL_RAN` despite `coreTools` scoping the allowlist to `run_shell_command(team)` only |

If B is FAIL, `run_shell_command` stays unrestricted. Read-only is enforced by
`excludeTools` alone, and a grunt could still mutate files via shell (e.g. `sed -i`).
This is an accepted, recorded risk — see the spec's "Still unverified" section.
