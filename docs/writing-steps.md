# Writing custom steps

A step is the unit of extension. Implementing one is small and self-contained.
This page covers the step contract and the simplest way to load a step — from
your config file. To ship steps as an installable package (with their own
config, default workflow, commands and doctor checks), wrap them in a plugin:
see [plugins.md](plugins.md).

## The contract

```python
from runspool.engine.step import Step, StepContext, StepResult


class MyStep(Step):
    name = "my_step"                       # unique; how workflows reference it

    def run(self, ctx: StepContext) -> StepResult:
        # ... do work ...
        return StepResult(message="done")
```

### `StepContext` (what you receive)

| Field | Description |
| --- | --- |
| `ctx.task` | The task row as a dict: `id`, `input`, `name`, `workflow`, `step`, `priority`, retry counters, `metadata` (a dict, `{}` if none), `parent_task_id`, … |
| `ctx.config` | The resolved engine settings (`AppConfig`, e.g. `ctx.config.workspace_root`). |
| `ctx.attempt` | Which run of this step this is: `1` the first time, plus one for every earlier run of this step by this task, whatever its outcome (deferred, failed, degraded, interrupted). |
| `ctx.should_stop()` | Returns `True` when **termination** was requested — check it during long loops and return early. Pause is not signalled here: it is applied at the step boundary, so a running step always finishes. |
| `ctx.heartbeat(progress=None)` | Refresh the heartbeat; pass a string to also record progress. Writes are throttled to ~1/second. (A timer keeps the heartbeat alive anyway; call this to report progress.) |

### `StepResult` (what you return)

| Field | Description |
| --- | --- |
| `message` | A short human-readable line. It is shown in notifications and kept as the step run's **note** (in the `status` timeline and the JSON `step_runs`). |
| `updates` | Optional task field updates, limited to `name`, `priority`, `max_retries`, `progress` and `metadata`. Anything else — status, step, retry bookkeeping, locks — fails the step on purpose: those belong to the state machine. `metadata` must be a dict and **replaces** the task's metadata, so merge yourself: `{**ctx.task["metadata"], "key": value}`. |
| `degraded` | `True` when the step returned without fully doing its job (an optional input was missing, a best-effort upload failed). The run is recorded as `degraded`, the workflow continues, and the task ends `partially_completed` instead of `completed`. |

Steps never touch the database. A step gets no store, no task service and no
state machine — only `StepContext` in and `StepResult` out. The runner writes
the updates (only while this worker still holds the task's claim) and the state
machine decides the transition. Anything a step needs beyond that (a
credential, a client) is handed to it when it is constructed, which is what
plugins do.

## Reading input and writing artifacts

Steps read the task's `input` and write files to the task's workspace. A helper
gives you an isolated per-task directory:

```python
from pathlib import Path
from runspool.builtin_steps.workspace import task_workspace

class IngestStep(Step):
    name = "ingest"

    def run(self, ctx: StepContext) -> StepResult:
        src = Path(ctx.task["input"])
        ws = task_workspace(ctx.config, ctx.task)     # workspace_root/tasks/<id>/
        (ws / "copy.txt").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return StepResult(message=f"ingested {src.name}")
```

Later steps in the same workflow read the artifacts earlier steps wrote. This is
how the built-in pipeline passes data: `ingest_file` writes `source.txt`,
`normalize_markdown` writes `normalized.md`, `summarize_text` reads it, and
`archive` moves the whole directory to `ready/<id>/`.

Small values a later step or a sub-task needs can also travel in `metadata`
(`runspool add --meta KEY=VALUE`, or a step's `updates`).

## Signalling outcomes

- **Success** — return a `StepResult`. The task advances to the next step.
- **Success, but degraded** — return `StepResult(message=..., degraded=True)`.
  Say why in `message`; it becomes the run's note.
- **Fail (will retry)** — raise any exception. The runner records the error and
  the state machine retries until `max_retries`, then routes to
  `manual_required`. Make the exception message actionable; it shows up in
  `runspool inspect`.
- **Not ready yet** — raise `StepDeferred(reason, delay_seconds=...)`. The task
  stays on the current step and is tried again **without** counting a failure.
  Use it to wait for a file to appear, a time window, or a hand-off.

```python
from runspool.engine.step import StepDeferred

class WaitForFile(Step):
    name = "wait_for_file"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        if not (ws / "ready.flag").exists():
            if ctx.attempt > 48:
                raise RuntimeError("ready.flag never appeared")   # give up: a real failure
            raise StepDeferred("waiting for ready.flag", delay_seconds=1800)
        return StepResult(message="flag found")
```

The `reason` is recorded on the task's `deferred` event and as the run's note.
With `delay_seconds > 0` the task is not picked up again before the delay passes
(its `next_retry_at`); `runspool wake <id>` cuts the wait short. With the
default `0` it is retried on the next scheduling round. Delayed deferrals are
driven by the `daemon`; a one-shot `run` leaves the task queued for later.

## Steps with side effects

Set `side_effect = True` on a step whose effects leave Runspool — it publishes,
uploads, sends a message, creates a draft on a platform:

```python
class PublishDraft(Step):
    name = "publish_draft"
    side_effect = True

    def run(self, ctx: StepContext) -> StepResult:
        ...
```

Such a step runs only after a human approves **that attempt**: the task waits in
`awaiting_approval` until `runspool approve <id>` (or `runspool reject <id>`).
A retry, a re-run after a deferral, or a re-run after a crash is a new attempt
and asks again, so write the step to be safe to repeat (e.g. update the draft it
created earlier rather than creating another). With approvals disabled, or the
approval policy set to `never`, the step is refused instead of run — it never
runs unapproved. The flag is read once, when the step is registered (for a step
loaded from config, when it is first imported); changing it on the object later
has no effect.

Plugins can add their own pre-execute policies — refuse a step before anyone is
asked, or tell the approver exactly what will leave the machine. See
[plugins.md](plugins.md).

## Conditional steps

Override `when()` to skip a step based on the task or config:

```python
class PublishStep(Step):
    name = "publish"

    def when(self, task, config) -> bool:
        return task["metadata"].get("publish") == "yes"

    def run(self, ctx): ...
```

A skipped step advances the workflow without running.

## Registering a step from config

The 0.1 way still works: put your step in a module, then point config at it.

```yaml
plugin_paths: [steps]                 # added to sys.path, relative to this config
steps:
  my_step:
    import: "my_module:MyStep"        # the config key MUST equal the step's name
workflows:
  example:
    steps: [my_step, archive]
```

The `steps:` map is loaded **lazily**: the names are reserved when Runspool
starts (so a clash with a built-in step is caught at once), but a module is
imported only when its step is first needed. Read-only commands such as
`status` never import your code; `run`, `daemon` and `doctor` import every
configured step up front, check each is a `Step` subclass (no-argument
constructor) whose `name` matches its key, and refuse to run (or report, for
`doctor`) if any fails.

For steps that need configuration, credentials, or their own commands, or that
you want to install with `pip`, write a plugin package instead:
[plugins.md](plugins.md).

## Testing a step

Steps are plain classes — test them directly or run a task through
`run_until_idle`:

```python
from runspool.commands import add_task
from runspool.runtime import run_until_idle

def test_my_workflow(ctx):                       # ctx fixture: see tests/conftest.py
    tid = add_task(ctx, "input-value", workflow="example")
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(tid)["task_status"] == "completed"
```

See the examples for complete, runnable references:
[client-intel-brief](../examples/client-intel-brief/steps/intel_steps.py)
(steps loaded from config) and
[runspool-example-creator](../plugins/runspool-example-creator/src/runspool_example_creator/steps.py)
(steps in a plugin package).
