# Concepts

Runspool is a small engine with a few well-separated parts. This page explains
the model so the CLI and the code make sense.

Since 0.2 every one of those parts — the store, the step registry, the
workflows, the task lifecycle, the runtime, the doctor, even the CLI's extra
commands — is a **plugin** mounted on a small kernel. That changes how Runspool
is assembled (see [Plugins](#plugins) below, [plugins.md](plugins.md) and
[architecture.md](architecture.md)), not what a task is or how it moves.

## Task

A **task** is one unit of work, stored as a row in SQLite. Its identity is its
`input` (a string — usually a file or directory path) and an auto-assigned `id`.
A task also carries a human-readable `name`, the `workflow` it belongs to, the
`step` it is currently on, its `task_status`, retry counters, a `priority`, and
timestamps. The `input` is immutable after creation; everything else can change
as the task progresses. (When the input names an existing file or directory,
`add` stores its absolute path, so steps find it whichever directory a later
`run` or the daemon starts in.)

A task can also carry:

- `metadata` — a free-form JSON object it was created with
  (`runspool add --meta KEY=VALUE`, or `tasks.add(..., metadata=...)` from a
  plugin). Steps read it as `ctx.task["metadata"]`.
- `parent_task_id` — the task it was created from (`--parent`), for sub-flows.

A task, its metadata and its `created` event are written in one transaction, so
nothing can claim a task before what it needs is recorded with it.

## Workflow

A **workflow** is an ordered list of step names, defined in config:

```yaml
workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]
```

A task advances through these steps in order. When the last step completes, the
task is `completed` (or `partially_completed`, see [Degraded runs](#degraded-runs-and-notes)).

A plugin can contribute a default workflow (the WeChat plugin adds
`wechat_article`); a workflow of the same name in your config always wins.

## Step

A **step** is a small class implementing one capability. It receives a
`StepContext` (the task, the resolved config, a stop check, a heartbeat
callback, and which `attempt` this is) and returns a `StepResult` (a message,
optional field updates, and a `degraded` flag). Steps read the task and write
artifacts to the filesystem; they never touch the database. See
[writing-steps.md](writing-steps.md).

A step can declare `side_effect = True` when its effects leave Runspool
(publishing, uploading, sending). Such a step runs only after a human approves
it — see [Approval](#approval-of-side-effect-steps).

Steps come from the **step registry**: the built-in steps, steps declared in the
config's `steps:` map, and steps registered by plugins.

## Task status

```
queued              waiting to be claimed (possibly until next_retry_at, after a deferral)
running             a worker is executing the current step
pause_pending       pause requested while running (applies at the step boundary)
paused              paused; resume returns it to queued
awaiting_approval   the next step has side effects and waits for approve / reject
failed              a step failed and an automatic retry is scheduled
manual_required     retries exhausted, or the step was refused; needs a human (or agent)
terminated          stopped permanently
completed           all steps done
partially_completed all steps ran, but at least one reported it was degraded
```

`completed`, `partially_completed` and `terminated` are terminal.

## Lifecycle

```
add ─► queued ─claim─► running ─┬─ step returned ──► queued (next step) ─► … ─► completed
                                │                                          └─► partially_completed
                                ├─ StepDeferred ───► queued, same step (not before next_retry_at;
                                │                    `wake` makes it runnable now)
                                ├─ step raised ────► failed ─(retry due)─► queued
                                │                └─► manual_required   (retries exhausted)
                                ├─ needs approval ─► awaiting_approval ─approve─► queued
                                │                                     └─reject──► manual_required
                                └─ gate refused ───► manual_required

running ─pause─► pause_pending ─(step boundary)─► paused ─resume─► queued
queued  ─pause─► paused
failed / manual_required ─retry─► queued
any non-terminal state ─terminate─► terminated   (a running step finishes first)
```

The full transition map lives in
[`state_machine.py`](../src/runspool/persistence/state_machine.py).

## State machine

All transition rules live in one place, the **state machine**. It is the only
component that decides "queued → running", "running → next step", "fail →
retry vs. manual_required", the approval and pause/resume/terminate transitions,
and what recovery does. It is also the only writer of status: every write is a
**compare-and-set** that applies only if the task is still in the state the
machine read (and, for a worker, only while that worker's claim is current). If
something else changed the task first, the machine re-reads and decides again.

User-initiated control actions are **guarded** there, not just in the CLI, so an
illegal transition is refused no matter who calls it (a script, a plugin, or an
AI agent included):

| Action | Allowed from |
| --- | --- |
| `pause` | `queued` (pauses at once), `running` (becomes `pause_pending`) |
| `resume` | `paused` |
| `retry` | `failed`, `manual_required` |
| `terminate` | any non-terminal state; a running task stops at the step boundary |
| `wake` | `queued` and waiting on a delay (`next_retry_at` set) |
| `approve` | `awaiting_approval`, for the step it is waiting on |
| `reject` | `awaiting_approval` |
| `set-step` | `failed`, `manual_required`; with `--force` also `queued`, `paused` — never `running`, `pause_pending`, `awaiting_approval` or a finished task |
| `set-retries` | any state (sets the ceiling and resets `retry_count`) |

For example, `retry` on a `completed` task is rejected rather than silently
re-queuing finished work. The valid actions for a task's current state are also
reported by `runspool inspect <id>` as `available_actions`.

## Step boundaries: pause and terminate

A running step is never interrupted. `pause` and `terminate` on a running task
set a flag; when the step finishes, the state machine decides the transition in
one place (`finish_step`), from the step's outcome and the flags:

| Step outcome | terminate requested | pause requested |
| --- | --- | --- |
| returned | `terminated` | advances to the next step, then `paused` (so the finished step is not re-run); on the last step, the task completes |
| raised `StepDeferred` | `terminated` | `paused` on the same step |
| raised, retries left | `terminated` (the error is kept in `last_error`) | the failure is recorded (`retry_count`, `last_error`), then `paused` instead of scheduling a retry |
| raised, retries exhausted | `terminated` | `manual_required` |
| needs approval | `terminated` | `paused` on the same step; approval is asked again after resume |
| refused by the gate | `terminated` | `manual_required` |
| requested **before the step started** (the job was still waiting in the worker pool) | `terminated`; the step never runs | `paused` on the same step; the step never runs |

**Terminate wins** over everything, including a pending pause. A step can poll
`ctx.should_stop()` to return early once termination is requested; pause is not
signalled, so a paused task always has a clean boundary.

A worker whose claim was taken away (its task was reclaimed and possibly claimed
again) changes nothing when it finishes: every transition it makes carries its
claim token, and its field updates are written only while it holds the claim.

## Deferral, delays and wake

A step that is not ready raises `StepDeferred(reason, delay_seconds=...)`. The
task goes back to `queued` on the **same step**, without counting a failure. The
`reason` is recorded on the `deferred` event and as the step run's note.

- `delay_seconds=0` — retried on the next scheduling round.
- `delay_seconds>0` — the task's `next_retry_at` is set; the coordinator skips
  it (silently, no events) until then. `inspect` shows the time and offers
  `wake`; `runspool wake <id>` clears the delay so it runs on the next round.

Pausing a waiting task drops its delay: on resume it is runnable at once.

## Degraded runs and notes

A best-effort step that returned without fully doing its job (an optional input
was missing, an optional upload failed) returns `StepResult(degraded=True)`. The
step run is recorded as `degraded`, the workflow carries on, and when the last
step finishes the task ends `partially_completed` instead of `completed`. Only
the latest run of each step counts: a later clean re-run of that step clears it.

Every step run can carry a **note**: the step's `StepResult.message`, or the
deferral reason. Notes appear in the step timeline of `runspool status <id>` and
as `note` in the JSON `step_runs`.

## Attempts

`ctx.attempt` tells a step which run of the current step this is: `1` the first
time, plus one for every earlier run of that step by this task, whatever its
outcome (deferred, failed, degraded, interrupted). A step can use it to give up
after N transient failures without keeping its own bookkeeping. Approvals are
also tied to an attempt (below).

## Approval of side-effect steps

Before any step runs, the runner asks a **gate** whether it may. Plugins can add
policies to it (`step/pre-execute`) and guards, which can refuse a step or ask for
approval; Runspool's own rule comes last and cannot be overridden: a step with
`side_effect = True` runs only if a human approved **this attempt of this step**.

If it has not been approved, the task moves to `awaiting_approval` without
holding a worker, and an `approval_asked` event records why. `runspool approve
<id>` grants that one attempt and requeues the task; `runspool reject <id>`
sends it to `manual_required` (`retry` asks again). Both write an
`approval_decided` event naming who decided. A retry, a re-run after a deferral,
or a re-run after a crash is a new attempt and asks again.

Approval **fails closed**: with the `approval` plugin disabled, or its policy set
to `never` (for unattended runs), side-effect steps are refused
(`manual_required`) rather than run. A gate or policy that errors refuses too.
The details are in [plugins.md](plugins.md) and
[architecture.md](architecture.md).

## Coordinator, worker pool, runner

- The **coordinator** runs one scheduling *tick*: it requeues failed tasks whose
  retry is due, scans queued tasks (skipping those waiting on a delay and those
  over their per-step concurrency quota), claims the rest with a fresh claim
  token, and submits them to the worker pool. Under the daemon it also reclaims
  tasks whose worker went silent past the heartbeat timeout.
- The **worker pool** is a bounded thread pool. Each submitted task runs in a
  worker thread.
- The **runner** executes a single step: it checks the claim is still current,
  honours a pause/terminate requested while the job waited, asks the gate, starts
  a `step_runs` record, runs the step, persists its allowed field updates, and
  reports the outcome to the state machine. Any exception becomes a clean
  failure — a step run never hangs in "running".

## Daemon vs. run

- `runspool run` performs ticks until no further progress is made, then exits.
  It's perfect for demos, batch jobs, and cron. A task deferred with a delay is
  left `queued` for a later `run` (or the daemon).
- `runspool daemon` runs a resident loop. It ticks on an interval, reclaims
  stalled tasks, and shuts down gracefully on SIGINT/SIGTERM. Use it for
  long-running steps, timed retries and delayed deferrals.

Both settle interrupted tasks when they start (see
[recovery](#heartbeats-reclaim-and-recovery)), which is why `run` refuses to
start while a daemon is live. Both also refuse to start when an enabled plugin
is not active or a configured step cannot be imported: a missing plugin might
have been meant to provide or override a step, and running without it could run
the wrong one. Read-only commands (`status`, `inspect`, …) still work.

## Persistence

Four tables, in one SQLite file under `workspace_root`:

- `tasks` — the task rows described above.
- `task_events` — an append-only log of every state change (`created`,
  `claimed`, `step_completed`, `step_failed`, `deferred`, `woken`, `paused`,
  `approval_asked`, `approval_decided`, `retry`, `reclaimed`, …). A transition
  and its events commit in the same transaction.
- `step_runs` — one row per step execution: status (`ok`, `degraded`,
  `deferred`, `failed`, `interrupted`), duration, error, and note.
- `schema_meta` — the schema version. Opening a database applies the migrations
  it has not seen (a 0.1 database is upgraded in place); a database written by a
  newer Runspool is refused.

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

## Heartbeats, reclaim and recovery

A running step's heartbeat is refreshed on a timer (and whenever the step calls
`ctx.heartbeat()`; writes are throttled to at most once per second). If a worker
dies, its task's heartbeat goes stale and the daemon **reclaims** it after
`worker_pool.heartbeat_timeout_seconds`. When `run` or the daemon starts, every
task left `running` or `pause_pending` by a previous process is **recovered**.

Reclaim and recovery settle a task the same way. The interrupted step is assumed
**not done** (its step run is closed as `interrupted`), and:

- a requested terminate wins: the task is `terminated`;
- a requested pause (or a task that was already `pause_pending`) is `paused` on
  the same step, so the step re-runs on resume;
- otherwise the task is requeued on the same step.

If the heartbeat moves while the daemon is deciding, the worker is alive after
all and the task is left alone. A re-run is a new attempt, so an approval does
not carry over. Nothing is lost across a crash; steps should be idempotent.

## Plugins

Runspool is assembled from plugins on a small kernel that knows nothing about
tasks. The default **profile** (your config file) mounts two bundles that ship
with Runspool: `core` (store, steps, workflows, tasks, runtime, doctor,
credentials, approval, CLI extensions) and `builtin-steps` (one plugin per
built-in step). Installed packages add more — such as the official
[`runspool-wechat`](../plugins/runspool-wechat/) — through entry points, and a
profile lists bundles, inserts or disables entries, and configures them (see
[workflows.md](workflows.md#the-config-file-is-a-profile)). Plugins can register
steps, default workflows, CLI commands, doctor checks and gate policies. The
core entries that carry the invariants above cannot be disabled; the store and
credentials are replaceable seams. Writing one: [plugins.md](plugins.md). How the
kernel works: [architecture.md](architecture.md).

## Credentials

Configuration never holds a secret, only its **name** (environment-variable
style, e.g. `WECHAT_APPSECRET`). A plugin resolves the name when it needs the
value. The default `credentials-local` provider looks it up in the process
environment, then `~/.config/runspool/credentials.yaml` (`$XDG_CONFIG_HOME` is
honoured; keep it owner-only), then a `.env` next to the profile, then
`~/.env`. Every lookup re-reads the sources, so a rotated secret needs no
restart. Values never go into logs, events or errors; `runspool doctor` reports
only whether each name is configured and where, and flags a credentials file
other users can read.
