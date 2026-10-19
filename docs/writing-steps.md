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
| `artifacts` | Optional: the artifacts the step produced, as `Artifact(type, path, sha256=None)` values, recorded in the database. See [Artifact contracts](#artifact-contracts). |

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

### Opting out of automatic retries

When a step raises, the task is retried while it has retries left
(`max_retries`), and the **same step runs again**. For a step whose effect may
already have happened when it raised (the remote call succeeded but the response
was lost, or a bug came after the call), set `retry = False`:

```python
class CreateDraft(Step):
    name = "create_draft"
    retry = False

    def run(self, ctx: StepContext) -> StepResult:
        ...
```

If such a step raises, the task goes straight to `manual_required` with the
error recorded, whatever retries it has left; it is never requeued on its own.
A person can still requeue it with `runspool retry <id>`. `StepDeferred` is not
a failure and behaves as before, and the pre-execute gate is unchanged. The
default is `retry = True`. A lazily loaded step answers from the real step, and
`runspool doctor` lists the steps that opted out (check `no_auto_retry`).

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

## Artifact contracts

Optional. A step can declare the artifact types it needs from earlier steps and
the ones it produces, and record what it actually produced. Steps that declare
nothing behave exactly as before and are not checked.

```python
from runspool.engine.artifacts import Artifact


class Adapt(Step):
    name = "adapt"
    requires = ("master",)              # needs a "master" from an earlier step
    provides = ("variant:<target>",)   # produces one variant per target

    def run(self, ctx: StepContext) -> StepResult:
        out = write_variant(ctx, target="blog")       # your code
        return StepResult(artifacts=[Artifact("variant:blog", out)])
```

**Types.** A type is a `kind` (`master`, `transcript`) or `kind:param`
(`variant:blog`). Kinds are lower-case letters, digits, `_`, `.` and `-`,
starting with a letter; a param is a value of letters, digits, `_`, `.` and `-`,
or a placeholder in angle brackets (`variant:<target>`) that stands for any
value. A requirement is met by an earlier step that provides:

| Required | Met by |
| --- | --- |
| `master` | `master` only (not `master:x`) |
| `variant:<target>` | any `variant:...` |
| `variant:blog` | `variant:blog`, or a placeholder `variant:<...>` |

Placeholder names are labels; they do not bind across steps. A workflow can
declare what the task's input provides before the first step with `inputs` (see
[Workflows](workflows.md#validation)).

**Checking the chain.** `runspool doctor` checks every workflow and shows an
`artifacts` check once any step or workflow declares a contract (or declares one
wrongly: `requires = 123` is an error, not "declares nothing"). `run` and the
daemon check a task's workflow before running each step of it: while a declared
requirement is not provided by an earlier step, the step is refused and the task
goes to `manual_required` with the reason; other workflows keep running. Fix the
workflow, then `runspool retry <id>`.

**Checked against the steps registered when the runtime started.** This is a
deliberate boundary. The plugin set is fixed once `run` or the daemon starts,
whether or not any workflow uses contracts: reloading a plugin
(`fiber.restart()`), loading or unloading one, providing or withdrawing a
service, and registering or unregistering a step all raise
`PluginChangeRefused` before anything changes, and `doctor` (in the same
process) reports a `plugin changes` check saying a restart is needed. Restart
`run` or the daemon for plugin changes to take effect, including a change that
adds the first contract.

A workflow none of whose steps declare anything is not checked. In a workflow
that does declare, a requirement that no earlier step provides is an error even
when an earlier step declares nothing. If you mix declared steps with older ones
on purpose, set `allow_unverified: true` on the workflow: such a gap is then
reported as *unverified* (by `doctor`, and once per process as a warning on the
`runspool.contracts` logger when its tasks run) instead of refused. Conditional
steps (`when`) are assumed to run. Applications that know their concrete values
(the actual targets) can check again with `runspool.engine.artifacts.check_chain`.

**Recording.** Each `Artifact` in `StepResult.artifacts` is stored with its type,
path, sha256 and size, tied to the step run. A relative `path` is relative to
`workspace_root` and is stored as given; non-ASCII paths are fine. Leave `sha256`
out and the runner hashes the file, which must exist; pass it for something the
runner cannot read (a directory you hashed yourself). The step fails, and none of
its artifacts are recorded, when an artifact has a malformed or placeholder type,
a type the step does not declare in `provides` (a step that declares nothing may
record any concrete type), a missing file to hash or a malformed `sha256`.
Errors name the artifact by position and type, never by path.

Rows are never updated: a step that runs again records again. Read them with
`runspool artifacts <id> [--all] --json`, or `store.artifacts.latest(task_id)` /
`list_for_task(task_id)` in code. Rows of a run that did not end `ok` or
`degraded` (it failed after recording, the process died) are left out unless you
pass `include_unfinished=True`.

## Spawning child tasks

Optional. A step can fan its task out: return the child tasks to create in
`StepResult.spawn`.

```python
from runspool.engine.children import ChildTask


class Fanout(Step):
    name = "fanout"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            message="one unit per target",
            spawn=[
                ChildTask(ctx.task["input"], "unit", name=target,
                          metadata={"target": target}, key=target)
                for target in ("blog", "newsletter")
            ],
        )
```

**Created with the step.** When the step succeeds (also when it is `degraded`),
the children are created in the same database transaction that advances the
parent, and so is the rest of that step's result: its `updates` and the end of
its step run (`ok` / `degraded`). A crash leaves either all of it (the parent
moved on, its children exist, its updates are written) or none of it (the run is
recovered as interrupted and the step runs again), so a re-run after a crash
never creates duplicates and never sees an update from the attempt that did not
complete (a `when` that reads one still runs the step). A stale worker whose
claim was taken back writes nothing of it. Nothing is spawned when the step fails
or defers, or when a terminate request ends the task at that boundary (its
updates are still written then, as for any step); a pause at the boundary does
not stop it. Artifacts are recorded before, tied to the run, and only listed once
that run has ended `ok` or `degraded`.

**Independent tasks.** Each child is a task of its own, queued on the first step
of its workflow, with `parent_task_id` set to the parent and the configured
`max_retries`. The parent does not wait for its children: it goes on to its next
step, or finishes. If something should happen once the children are done, give
the children a step for it (or poll `store.repo.list_children(parent_id)`).

**Keys.** `key` names a child within its parent. If the spawning step runs again
(after `runspool set-step` back to it), a child whose key the parent already
spawned is not created again; children without a key are created every time the
step succeeds. Each spawn is recorded (which step run of the parent spawned which
child, and its key), and the parent gets a `children_spawned` event listing the
ids created and those already spawned.

**Validation.** The spawn is checked before anything of the result is written
(artifacts, updates): it must be a list or tuple (an empty one spawns nothing;
anything else fails, even an empty or falsy value such as `{}`, `0` or `""`), and
every entry a `ChildTask` with a non-empty `input`, a defined `workflow`, a string
`name`, a `metadata` that can be stored as a JSON object (string keys: it is
stored with its keys sorted, and checked the same way) and a non-empty `key` used
once. A spawn that does not validate fails the step (the error names the child by
position, never its input) and nothing is created. Every text a child is stored
with must also be valid UTF-8 (a lone surrogate, as decoded JSON or a YAML
`\uD800` escape can produce, is refused, without echoing it): its `input`, its
`workflow` name, the first step of that workflow, its `name`, its `key` and its
serialized `metadata`. The result's `updates` are not checked this way: if they
cannot be stored, the transaction below rolls back. If the store still
fails while writing the transaction, it rolls back (no child, no update) and the
step fails rather than leaving the task running. If the transaction committed
and only reading the task back afterwards fails, nothing is undone: the failure
is reported, and the committed run, children and artifacts stay as they are. An external step's validator may return
a spawn too: the children are created when the submission is accepted.

## External steps

Optional. Some steps are done outside the engine: an AI agent writes a draft, a
person checks it. Subclass `ExternalStep`: instead of `run`, it says what it asks
for (`request`) and how to check what comes back (`validate`).

```python
from runspool.engine.artifacts import Artifact
from runspool.engine.external import ExternalStep, SubmissionRejected, WorkRequest


class Proofread(ExternalStep):
    name = "proofread"
    max_attempts = 3                      # rejections before a human is needed

    def request(self, ctx: StepContext) -> WorkRequest:
        return WorkRequest(
            kind="human",                 # a free label for clients
            instructions="Proofread the draft and save the result as a file.",
            payload={"draft": ctx.task["input"], "attempt": ctx.attempt},
        )

    def validate(self, ctx: StepContext, submission) -> StepResult:
        path = submission.file(ctx.config.workspace_root)   # checks sha256 if given
        if not path.read_text(encoding="utf-8").strip():
            raise SubmissionRejected("the file is empty")
        return StepResult(message="proofread", artifacts=[Artifact("proofread", path)])
```

**How it runs.** When a task reaches the step, the engine opens a *work item*
from `request(ctx)` and the task waits in `awaiting_external` (no worker is
held). A client then:

1. lists open items (`runspool items`) and reads one (`runspool items show <id>`:
   `instructions`, `payload`, and `previous_error` from a rejected attempt);
2. claims it (`runspool items claim <id>`): of several clients racing for an
   item exactly one wins, and gets a claim token;
3. submits a JSON object with that token (`runspool items submit <id> --token T
   --file PATH`, which submits `{"path", "sha256"}`; `--value TEXT` submits
   `{"value": TEXT}`; `--json-value` any object).

Submitting only records the submission and queues the task. Validation runs in
the executor (`run` or the daemon), like any step code, after the start-up checks
and under the executor lock; it never runs in the submitting process.

**Validation.** `validate(ctx, submission)` gets a `Submission` (`item_id`,
`attempt`, `value`, `submitted_by`):

- return a `StepResult`: accepted. The workflow advances exactly as if the step
  had run and returned it (`updates`, `artifacts`, `degraded`, `message`); the
  check is recorded as a step run.
- raise `SubmissionRejected(reason)`: that attempt is used up (a step run with
  status `rejected`). A new item opens for the next attempt with the reason as
  `previous_error`. After `max_attempts` rejections the task goes to
  `manual_required`; `runspool retry` starts again at attempt 1. Rejections do
  not count against `max_retries`.
- raise anything else: an ordinary step failure (retried per `max_retries`). The
  submission is kept and validated again on the next try.

`Submission.file(root)` resolves `value["path"]` (relative to `root`), and rejects
the submission when the path is missing, the file does not exist or cannot be read
(permission denied, removed while being read: the reason names only the kind of
error), or a given `sha256` does not match; its reasons never contain the path.

An external step can declare `requires` / `provides` like any step, and what its
validator records is checked against `provides`. `side_effect = True` asks for
approval before the item is opened. A task that ends in any terminal state (for
instance when a last external step is skipped because its `when` no longer
holds), is terminated, or moves to another step cancels its remaining active item,
in the same transaction; a waiting task cannot be paused.

**Known limits.**

- *Claims do not expire.* If a claimant goes away (it crashed, the person left),
  the item stays `claimed` until someone runs `runspool items release <id>
  --force`. Expiring claims automatically would need lease renewal and a way to
  fence off the old claimant; without that, releasing on a timeout could have the
  same external work done twice.
- *Validation is not immediate.* A submitted task is queued again, and its
  submission is checked the next time the task can be scheduled (by `runspool
  run` or the daemon). There is no upper bound on how long that takes: other
  queued tasks, the step's concurrency quota (another validation of the same step
  holding its slot) and approval can each delay it further.
- *Submissions have no size limit.* The submitted JSON object is stored as it is;
  the command line and library are a local, trusted boundary. Submit large
  content as a file (`--file`, path plus sha256) rather than inline. A
  configurable byte limit may come later.

Clients that are not the command line use the same functions:
`runspool.commands.claim_item`, `release_item`, `submit_item` and `get_item`, and
`store.items` (`list_items`, `active_for`) to read.

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
