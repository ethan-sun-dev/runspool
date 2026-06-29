# Notes for AI agents and automation

Runspool is designed to be driven by automated callers. This file is a quick
orientation; see [docs/agent-json-output.md](docs/agent-json-output.md) for the
full schema.

## The loop

1. Queue work: `runspool add <input> --workflow <name>`
2. Advance it: `runspool run` (one-shot) or `runspool daemon` (resident)
3. Read state: `runspool inspect <id> --json`
4. Decide from the result:
   - `status: "completed"` → done; artifacts are listed in `artifacts`.
   - `status: "manual_required"` → read `last_error` and
     `suggested_next_action`, fix the cause, then run the suggested command
     (often `runspool retry <id>`), and advance again.
   - `status: "running"` / `"queued"` → wait and poll again.

## Rules of thumb

- Use `--json` on every read command; never parse human-formatted output.
- Choose actions only from the task's `available_actions`.
- `inspect`'s `suggested_next_action` already encodes the recommended recovery.
- Treat `completed` and `terminated` as terminal.
- All state is local under `workspace_root`; nothing is sent anywhere.

## Read commands with `--json`

`status`, `status <id>`, `inspect <id>`, `logs <id>`, `overview`, `workflows`,
`doctor`, `run`.
