# Concepts

Runspool is a small engine with a few well-separated parts. This page explains
the model so the CLI and the code make sense.

## Task

A **task** is one unit of work, stored as a row in SQLite. Its identity is its
`input` (a string — usually a file or directory path) and an auto-assigned `id`.
A task also carries a human-readable `name`, the `workflow` it belongs to, the
`step` it is currently on, its `task_status`, retry counters, a `priority`, and
timestamps. The `input` is immutable after creation; everything else can change
as the task progresses.

## Workflow

A **workflow** is an ordered list of step names, defined in config:

```yaml
workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]
```

A task advances through these steps in order. When the last step completes, the
task is `completed`.

## Step

A **step** is a small class implementing one capability. It receives a
`StepContext` (the task, the resolved config, a stop check, and a heartbeat
callback) and returns a `StepResult` (a message plus optional field updates).
Steps read the task and write artifacts to the filesystem; they never touch the
database directly. See [writing-steps.md](writing-steps.md).

Steps come from the **registry**: built-in steps plus any plugins you configure.

## Task status

```
queued            waiting to be claimed
running           a worker is executing the current step
pause_pending     pause requested while running (applies after the step)
paused            paused; resume returns it to queued
failed            a step failed and a retry is pending
manual_required   retries exhausted; needs a human (or agent) to intervene
terminated        stopped permanently
completed          all steps done
```

`completed` and `terminated` are terminal. The full transition map lives in
[`state_machine.py`](../src/runspool/persistence/state_machine.py).

## State machine

All transition rules live in one place, the **state machine**. It is the only
component that decides "queued → running", "running → next step", "fail →
retry vs. manual_required", and the pause/resume/terminate transitions. Keeping
these rules in one module is what makes the lifecycle predictable.

User-initiated control actions are **guarded** there, not just in the CLI, so an
illegal transition is refused no matter who calls it (a script or an AI agent
included):

| Action | Allowed from |
| --- | --- |
| `pause` | `queued`, `running` |
| `resume` | `paused` |
| `retry` | `failed`, `manual_required` |
| `terminate` | any non-terminal state (refused on `completed` / `terminated`) |
| `set-step` | `failed`, `manual_required` (or `--force`) |

For example, `retry` on a `completed` task is rejected rather than silently
re-queuing finished work. The valid actions for a task's current state are also
reported by `runspool inspect <id>` as `available_actions`.

## Coordinator, worker pool, runner

- The **coordinator** runs one scheduling *tick*: it scans queued tasks, skips
  any that are over their per-step concurrency quota, claims the rest, and
  submits them to the worker pool. It also reclaims tasks whose worker went
  silent past the heartbeat timeout.
- The **worker pool** is a bounded thread pool. Each submitted task runs in a
  worker thread.
- The **runner** executes a single step: it starts a `step_runs` record, runs
  the step, persists any field updates, and applies the resulting transition
  (advance, defer, fail, pause, or terminate). Any exception becomes a clean
  failure — a step run never hangs in "running".

## Daemon vs. run

- `runspool run` performs ticks until no further progress is made, then exits.
  It's perfect for demos, batch jobs, and cron.
- `runspool daemon` runs a resident loop. It recovers interrupted tasks on
  startup, ticks on an interval, and shuts down gracefully on SIGINT/SIGTERM.
  Use it for long-running steps and steps that defer until a precondition holds.

## Persistence

Three tables, all under `workspace_root`:

- `tasks` — the task rows described above.
- `task_events` — an append-only log of every state change (`created`,
  `claimed`, `step_completed`, `step_failed`, `paused`, `retry`, …).
- `step_runs` — one row per step execution, with status, duration, and error.

The filesystem is the artifact store: each task gets
`workspace_root/tasks/<id>/`, and `archive` moves a finished task to
`workspace_root/ready/<id>/`.

## Retries

When a step raises, the state machine records the error and increments
`retry_count`. While `retry_count <= max_retries`, the task becomes `failed` with
a scheduled retry time (`now + retry_delay_seconds`); once retries are exhausted
it becomes `manual_required`. On each tick the coordinator requeues `failed`
tasks whose retry time has arrived, so retries happen **automatically** — both
under `runspool run` and the `daemon`.

- `retry_delay_seconds: 0` (the default) retries on the next tick, so a single
  `runspool run` consumes the whole retry budget and ends at `completed` or
  `manual_required`.
- A positive `retry_delay_seconds` delays each retry (backoff); those timed
  retries are driven by the long-running `daemon`.
- `max_retries: 0` makes the first failure terminal — it goes straight to
  `manual_required`. Use it when a failure means "bad input", not "transient".

A step can also raise `StepDeferred` to retry **without** consuming the budget —
use that for waiting on a precondition rather than for errors.

## Heartbeats and recovery

A running step periodically calls `ctx.heartbeat()` (the runner throttles writes
to at most once per second). If a worker dies, the task's heartbeat goes stale;
the coordinator reclaims it back to `queued` after
`worker_pool.heartbeat_timeout_seconds`. On daemon startup, any task left
`running` from a previous process is requeued. Nothing is lost across a crash.
