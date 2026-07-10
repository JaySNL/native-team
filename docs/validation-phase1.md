# Phase-1 validation results

| # | Question | Verdict | Evidence |
|---|---|---|---|
| A | Background Bash re-invokes an idle lead | PASS | controller observed run_in_background task-notification re-invoking an idle session, 2026-07-10 |
| B | qwen honours `coreTools: ["run_shell_command(team)"]` | FAIL | `echo SHELL_RAN` was executed — pane showed `✓ Shell echo SHELL_RAN ... SHELL_RAN` despite `coreTools` scoping the allowlist to `run_shell_command(team)` only |

B failed, so `run_shell_command` stays unrestricted. The conclusion drawn at the
time — "read-only is enforced by `excludeTools` alone" — was **also wrong**: see
row I. Nothing enforces read-only. Containment is positional: the grunt pane's
cwd is its own worktree.

| C | A real qwen grunt round-trips a task through the bus | PASS | live 2026-07-10: `team send` → grunt ran `team result add`/`done` → backgrounded `team wait --for lead` exited 0 |
| D | Verification catches a real fabrication | PASS | live: qwen cited `team/protocol.py:10` for `TEMPLATE`, which is on line 8 → `FABRICATED` |
| E | `pane-died` hook reports a dead grunt | PASS | live: killed the pane's process with `remain-on-exit on`; `failed` message reached the lead's inbox in ~3s |

| F | Verification separates a wrong quote from a wrong answer | PASS | live: grunt1 named the right two methods for "where is zombie damage applied to a building", and got **both** citations wrong — `CharacterFightHandler.cs:508` for `GetDamage` (actual 388) and a `Structure.cs:1575` quote missing its trailing `;` |
| G | Wrong line numbers are a tool-selection bug, not a competence ceiling | PASS | A/B on one cold question: with no instruction the grunt called `Read`, quoted both lines byte-perfectly, cited them **−4** and **+228** off; told to use `grep -n` it called its search tool and cited both **exactly** |
| H | The `grep -n` rule works from the protocol alone | PASS | grunt2, cold, no hint in the question, 3/3 PASS on `Structure.cs:1196/1206/1216` — matched against the operator's own `grep -n` |

Across the two cold tasks run before G, six citations: **every quoted source line was
correct and every line number was wrong.** The grunts are not bad at reading code. They
are bad at counting it, because a `Read` tool cannot tell them where they are. This is
why the instruction lives in `protocol.py` and not in the lead's question — the protocol
ships with every task, and a lead who has never watched a grunt's pane has no way to
know it is needed.

H is a single A/B, n=2 per condition. It is a clean confirmation of a hypothesis, not a
measurement. And it does not make citations trustworthy: it makes them *usually* right,
which is exactly the regime in which an unverified pipeline is most dangerous, because
it teaches the lead to stop checking. `verify` still fails closed.

Observed in H and not enforced anywhere: scope said `Structure.cs`, and the grunt also
read `Health.cs`. "Do not wander" is advice, not a constraint. Citations outside the
repo are caught by `OUT_OF_TREE`; citations outside *scope* are not caught at all.

D is the project's reason to exist, reproduced against a real model on the first
task ever sent. It is also why `team verify` now fails closed: at the time of the
live run it printed `FABRICATED` and exited `0`.

F is the sharper result. The grunt's *answer* was correct — damage is decided in
`CharacterFightHandler.GetDamage` and lands via `Structure.SubtractHp` →
`Health.SubtractHp`. Its *citations* were 0/2. A lead reading the prose would have
believed the line numbers. This is the failure the tool exists to catch, and it is
not fabrication of the answer: it is fabrication of the evidence for a true answer,
which is harder to notice and worse to inherit.

F also exposed a verifier bug: the missing semicolon was reported as `FABRICATED`
("evidence appears nowhere in the file") when the line was right there. `TRUNCATED`
now names that case. Both remain failures.

| I | `excludeTools` blocks qwen's write tools | FAIL | live task 013: pane trace shows `✓ WriteFile Writing to probe/WaveTally.cs` four times, with `"excludeTools": ["write_file", …]` on disk |
| J | A build task's containment check sees everything the grunt wrote | FAIL | live task 013: the grunt's first `WriteFile` landed in the **main tree** (qwen's project root = the pane's cwd); `verify_build` inspects only the worktree and reported `CONTAINMENT` for a different, worktree-local file while never seeing this one |
| K | A build grunt fixes its own compiler errors and stays in bounds | PASS | live 013: the declared `probe/WaveTally.cs` compiles (`Build succeeded.`) and its citation verifies exactly |
| L | `bus_root()` finds the outer bus from inside a worktree | PASS | live 013: `team result add` run from `.team/work/grunt1` reached `<root>/.team/staging` |

I and J are the same root cause and are fixed together in the worktree spec's
Amendment 1: the grunt pane's cwd is now its worktree, `verify_build` gained
`ESCAPED`, and no document in this repo claims a grunt lacks write tools.
