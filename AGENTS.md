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
   - `status: "partially_completed"` → done, but a step reported it could not
     fully do its job; the `note` of its `degraded` step run says why.
   - `status: "awaiting_approval"` → the next step has side effects outside
     RunSpool (it publishes, uploads, sends). **Do not approve it yourself**:
     show `suggested_next_action` to a person, who runs `runspool approve <id>`
     or `runspool reject <id>`.
   - `status: "manual_required"` → read `last_error` and
     `suggested_next_action`, fix the cause, then run the suggested command
     (often `runspool retry <id>`), and advance again.
   - `status: "running"` / `"queued"` → wait and poll again. A queued task with
     `next_retry_at` is waiting on a deferral; `runspool wake <id>` runs it now.

## Rules of thumb

- Use `--json` on every read command; never parse human-formatted output.
- Choose actions only from the task's `available_actions`.
- `inspect`'s `suggested_next_action` already encodes the recommended recovery.
- Treat `completed`, `partially_completed` and `terminated` as terminal.
- `runspool doctor --json` exits non-zero when any check fails.
- All state is local under `workspace_root`; nothing is sent anywhere.

## Read commands with `--json`

`status`, `status <id>`, `inspect <id>`, `logs <id>`, `overview`, `workflows`,
`doctor`, `run`.
