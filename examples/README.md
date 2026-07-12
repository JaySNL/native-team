# examples/ — you build these

TeamBus ships the **bus comms**. Two things a real deployment wants are deliberately *not* wired
for you, because they encode your policy, not the tool's:

- **`guardrails/`** — an optional Claude Code hook that stops the lead from reading into a scope a
  grunt is actively working, so the saving of delegating the read is not paid twice. Reference
  implementation + wiring instructions. `team init` does **not** install it.
- **`TEAM_GRUNT_CONTEXT.md`** — a generic sample of the behavioral rules a grunt loads at boot.
  Copy it to the root of the repo your team works in (or symlink your own) to activate it; absent,
  grunts still run, just without standing instructions.

Nothing here is required to make the bus work. Start without them; add them when you want tighter
control over what the lead reads and how the grunt behaves.
