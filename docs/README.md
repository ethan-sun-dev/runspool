# Runspool documentation

- [Concepts](concepts.md) — the model: tasks, workflows, steps, the lifecycle
  and state machine, pause/terminate at step boundaries, deferral, approvals,
  recovery, plugins and credentials in brief.
- [Design decisions](design-decisions.md) — *why* it's built this way: the
  trade-offs taken and the alternatives rejected.
- [CLI reference](cli.md) — every command and its options, including commands
  contributed by plugins.
- [Workflows & configuration](workflows.md) — the config file in full: engine
  settings, and the profile keys that choose plugins.
- [Writing custom steps](writing-steps.md) — the step contract and loading steps
  from config.
- [Writing plugins](plugins.md) — packaging steps, workflows, commands, checks
  and policies as an installable plugin.
- [Architecture](architecture.md) — the kernel and the core plugins: how
  Runspool is assembled and where its invariants are enforced.
- [JSON output for scripts & agents](agent-json-output.md) — `--json` and the
  `inspect` schema.
- [Examples](examples.md) — the three runnable examples.

New here? Start with the [project README](../README.md) quickstart, then read
[Concepts](concepts.md).
