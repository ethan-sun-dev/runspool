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
| `artifacts` | Files produced so far, as paths relative to `workspace_root`. |
| `available_actions` | The control commands valid for the current status. |
| `suggested_next_action` | A plain-language recommendation (includes `last_error` when relevant). |

### `available_actions` by status

| Status | Actions |
| --- | --- |
| `queued` | `pause`, `terminate`, `set-priority` |
| `running` | `pause`, `terminate` |
| `paused` | `resume`, `terminate` |
| `failed` | `retry`, `set-step`, `terminate` |
| `manual_required` | `retry`, `set-step`, `set-retries`, `terminate` |
| `completed` / `terminated` | (none) |

## Other JSON commands

```bash
runspool status --json        # array of task objects
runspool status <id> --json   # one task + events + step_runs
runspool logs <id> --json     # array of events
runspool overview --json      # {"queued": 2, "completed": 5, ...}
runspool workflows --json     # {"local_file": ["ingest_file", ...]}
runspool doctor --json        # array of {name, ok, detail}
runspool run --json           # {"rounds": N, "tasks": [...]}
```

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
