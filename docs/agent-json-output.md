# JSON output for scripts and AI agents

Runspool is CLI-first and designed to be driven by non-humans too: shell
scripts, automation, and AI agents. Every read command supports `--json`, and
`inspect` is purpose-built for an agent's decision loop.

## The decision loop

```
add a task ──► runspool run (or daemon)
                     │
                     ▼
            runspool inspect <id> --json
                     │
        ┌────────────┼─────────────────────┐
   completed     manual_required          running
     done        act on available_actions  wait / poll
                 + suggested_next_action
```

An agent never needs to parse human text. It reads structured state, chooses
from `available_actions`, optionally follows `suggested_next_action`, and acts.

## `inspect --json` schema

```json
{
  "id": 1,
  "name": "Northwind Trading",
  "input": "./data",
  "status": "manual_required",
  "workflow": "client_intel",
  "current_step": "collect_sources",
  "priority": 0,
  "retry_count": 1,
  "max_retries": 0,
  "progress": null,
  "last_error": "FileNotFoundError: Missing required source(s): requirements.md",
  "recent_events": [
    {"event_type": "manual_required", "step": "collect_sources", "message": "...", "created_at": "2026-06-29 02:10:00"}
  ],
  "step_runs": [
    {"step": "collect_sources", "status": "failed", "duration_ms": 1, "error": "...", "started_at": "...", "finished_at": "..."}
  ],
  "artifacts": ["ready/1/..."],
  "available_actions": ["retry", "set-step", "set-retries", "terminate"],
  "suggested_next_action": "... Resolve the cause, then run `runspool retry 1`."
}
```

| Field | Meaning |
| --- | --- |
| `status` | One of the task statuses (see [concepts.md](concepts.md)). |
| `current_step` | The step the task is on. |
| `last_error` | The most recent failure message, or `null`. |
| `next_retry_at` | For a deferred task waiting on a delay: the UTC time it becomes runnable again (`runspool wake <id>` makes it runnable now). For a `failed` task: when its automatic retry is due. Otherwise `null`. |
| `parent_task_id` | The task this one was created from (a sub-flow), or `null`. |
| `metadata` | Free-form data the task was created with (an object; `{}` if none). |
| `artifacts` | Files produced so far, as paths relative to `workspace_root`. |
| `available_actions` | The control commands valid for the current status. |
| `suggested_next_action` | A plain-language recommendation (includes `last_error` when relevant). |

### `available_actions` by status

| Status | Actions |
| --- | --- |
| `queued` | `pause`, `terminate`, `set-priority` (plus `wake` first, when it is waiting on a delay) |
| `running` | `pause`, `terminate` |
| `paused` | `resume`, `terminate` |
| `failed` | `retry`, `set-step`, `terminate` |
| `manual_required` | `retry`, `set-step`, `set-retries`, `terminate` |
| `awaiting_approval` | `approve`, `reject`, `terminate` |
| `awaiting_external` | `items`, `terminate` |
| `completed` / `partially_completed` / `terminated` | (none) |

`partially_completed` means every step ran but at least one reported that it could
not fully do its job; the `note` of its degraded step run says why.

`awaiting_approval` means the next step has side effects outside RunSpool (it
publishes, uploads, sends) and must be approved by a human first. An automated
caller should not approve on its own; surface `suggested_next_action` to a person.

`awaiting_external` means the current step is done outside the engine: its work
item (see `runspool items show <id>`, named in `suggested_next_action`) says what
to do. An agent claims it (`runspool items claim <id> --json` gives the
`claim_token`), does the work, and submits it (`runspool items submit <id> --token
T --file PATH`); the step checks it when the task next runs. A rejected submission
opens the next attempt, whose `previous_error` says what to fix.

## Other JSON commands

```bash
runspool status --json        # array of every task (add -n N / --active / --status to filter)
runspool status <id> --json   # one task + events + step_runs
runspool logs <id> --json     # array of events
runspool artifacts <id> --json  # recorded artifacts: [{type, path, sha256, ...}]
runspool events --since N --json  # {"events": [...], "cursor": M}: events after id N, oldest first
runspool overview --json      # {"queued": 2, "completed": 5, ...}
runspool workflows --json     # {"local_file": ["ingest_file", ...]}
runspool doctor --json        # array of {name, ok, detail}
runspool plugins --json       # {"plugins": [...]}: entries, state, metadata, config schema
runspool run --json           # {"rounds": N, "tasks": [...]}
```

### Following progress from another process

Event ids only ever increase. A client that is not the process running the tasks
(another terminal, a UI, an agent) can follow everything that happens by keeping
the last cursor it saw:

```bash
cursor=0
while true; do
  feed=$(runspool events --since "$cursor" --json)
  echo "$feed" | jq -c '.events[]'
  cursor=$(echo "$feed" | jq '.cursor')
  sleep 2
done
```

Each event carries `id`, `task_id`, `event_type`, `step`, `message`, `payload`
and `created_at`. Add `--task <id>` to follow one task.

## Example: a tiny agent loop in bash

```bash
runspool add ./data --workflow client_intel
runspool run >/dev/null

state=$(runspool inspect 1 --json)
status=$(echo "$state" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")

if [ "$status" = "manual_required" ]; then
  echo "$state" | python3 -c "import json,sys;print(json.load(sys.stdin)['suggested_next_action'])"
  # ... fix the cause (e.g. add the missing file) ...
  runspool retry 1
  runspool run
fi
```

## Design guarantees

- Field names are stable; new fields may be added, existing ones won't silently
  change meaning.
- `--json` writes a single JSON document to stdout and nothing else, so it pipes
  cleanly into `jq`, `python -m json.tool`, or an agent's parser.
