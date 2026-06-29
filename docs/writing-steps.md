# Writing custom steps

A step is the unit of extension. Implementing one is small and self-contained.

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
| `ctx.task` | The task row as a dict: `id`, `input`, `name`, `workflow`, `step`, `priority`, retry counters, … |
| `ctx.config` | The resolved `AppConfig` (e.g. `ctx.config.workspace_root`). |
| `ctx.should_stop()` | Returns `True` when **termination** was requested — check it during long loops and return early. Pause is not signalled here: it is applied at the step boundary, so a running step always finishes. |
| `ctx.heartbeat(progress=None)` | Refresh the heartbeat; pass a string to also record progress. Writes are throttled to ~1/second. |

### `StepResult` (what you return)

| Field | Description |
| --- | --- |
| `message` | A short human-readable line shown in logs/notifications. |
| `updates` | Optional task field updates. Only whitelisted columns are allowed (e.g. `name`, `priority`); anything else fails the step on purpose. |

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

## Signalling outcomes

- **Success** — return a `StepResult`. The task advances to the next step.
- **Fail (will retry)** — raise any exception. The runner records the error and
  the state machine retries until `max_retries`, then routes to
  `manual_required`. Make the exception message actionable; it shows up in
  `runspool inspect`.
- **Not ready yet** — raise `StepDeferred`. The task stays on the current step
  and is retried on the next tick **without** counting a failure. Use it to wait
  for a file to appear, a time window, or a manual hand-off.

```python
from runspool.engine.step import StepDeferred

class WaitForApproval(Step):
    name = "wait_for_approval"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        if not (ws / "approved.flag").exists():
            raise StepDeferred()              # check again next tick
        return StepResult(message="approved")
```

(Deferring steps need the `daemon`, which keeps ticking; one-shot `run` stops
once nothing else can progress.)

## Conditional steps

Override `when()` to skip a step based on the task or config:

```python
class PublishStep(Step):
    name = "publish"

    def when(self, task, config) -> bool:
        return bool(getattr(config, "publish_enabled", False))

    def run(self, ctx): ...
```

A skipped step advances the workflow without running.

## Registering a step

Put your step in a module, then point config at it:

```yaml
plugin_paths: [steps]                 # added to sys.path, relative to this config
steps:
  my_step:
    import: "my_module:MyStep"        # the config key MUST equal the step's name
workflows:
  example:
    steps: [my_step, archive]
```

Runspool imports the module, instantiates the class (a no-argument constructor),
checks it is a `Step` subclass whose `name` matches the config key, and registers
it. `runspool doctor` verifies all of this up front.

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

See the example plugins for complete, runnable references:
[client-intel-brief](../examples/client-intel-brief/steps/intel_steps.py) and
[creator-publishing-pipeline](../examples/creator-publishing-pipeline/steps/creator_steps.py).
