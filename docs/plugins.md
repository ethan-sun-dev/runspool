# Writing plugins

Everything RunSpool 0.2 does is a plugin: the store, the state machine, the
scheduler, the CLI, approvals, credentials, the built-in steps. Your plugin uses
the same mechanism; there is no back door and no second-class API. This page is
the guide for plugin authors. [architecture.md](architecture.md) explains how the
pieces fit; [writing-steps.md](writing-steps.md) covers the step contract itself.

The running examples are [`runspool-example-creator`](../plugins/runspool-example-creator/)
(steps and a default workflow) and [`runspool-wechat`](../plugins/runspool-wechat/)
(config, credentials, an approved step with side effects, a `step/pre-execute`
policy, CLI commands, a doctor check).

## A first plugin

The creator example, complete:

```python
from runspool.kernel import Plugin
from runspool_example_creator.steps import (
    CollectMaterialsStep, CreatePublishChecklistStep, DraftArticleStep,
    ExtractHighlightsStep, RenderPlatformPackageStep,
)

STEPS = (CollectMaterialsStep, ExtractHighlightsStep, DraftArticleStep,
         RenderPlatformPackageStep, CreatePublishChecklistStep)
WORKFLOW = [cls.name for cls in STEPS] + ["archive"]

def apply(ctx, config) -> None:
    for cls in STEPS:
        ctx.steps.register(cls())
    ctx.workflows.add_default("creator_publishing", WORKFLOW)

plugin = Plugin(name="example-creator", apply=apply, inject=["steps", "workflows"])
```

`inject` names the services the plugin needs. The kernel loads the plugin once
they are all available and calls `apply(ctx, config)`. Each registration made
through `ctx` is recorded and undone automatically when the plugin unloads.

## Plugin shapes

The kernel accepts any of these:

| Shape | How it is called |
| --- | --- |
| `Plugin(name, apply, Config=None, inject=())` | `apply(ctx, config)`. Recommended for packaged plugins. |
| An object or module with an `apply` attribute | `obj.apply(ctx, config)` |
| A class | `cls(ctx, config)`; the instance's `dispose()`, if any, runs on unload |
| A plain function | `fn(ctx, config)` |

For the last three, optional metadata is read from attributes: `name`, `Config`
and `inject`. `inject` must be a sequence of names, never a single string.
Prefer `Plugin(...)`: it states name, config model and dependencies in one place.

## Configuration

Give the plugin a pydantic model as `Config`. The kernel validates the entry's
config against it, fills defaults, and passes the model instance to `apply`. A
missing config validates as `{}`. From the WeChat plugin:

```python
from pydantic import BaseModel
from runspool.core.credentials import CredentialRef

class WechatConfig(BaseModel):
    appid: str | None = None
    appid_credential: CredentialRef = "WECHAT_APPID"
    appsecret: CredentialRef = "WECHAT_APPSECRET"
    author: str = ""
    allow_problems: bool = False
    workflow: str = "wechat_article"
```

Invalid config raises `runspool.kernel.ConfigError` and the plugin ends up FAILED.
The message names the fields and what is wrong, never the values (a config may
hold a secret pasted where a name belongs): `invalid config: appsecret: String
should match pattern '^[A-Z_][A-Z0-9_]*$'`.

Do not confuse the two configs a plugin sees: `config` (the `apply` argument) is
the plugin's own, while `ctx.config` is the `config` service, RunSpool's engine
settings (`workspace_root`, `workflows`, ...).

## What a plugin can reach

`ctx.<name>` returns a service only if the plugin declared `name` in `inject`, or
if the plugin itself or one of its ancestors other than the root provides it.
Anything else raises `ServiceNotInjected`, so a plugin's dependencies are
explicit. Services provided by the root (`config`, `startup`) must be injected
like any other.

For an optional dependency, use `ctx.get(name)`. It needs no `inject` and returns
the service if it is usable right now, or `None`. It does not wait for the
service, and the plugin is not reloaded when that service changes. Inject what
you cannot work without.

A plugin waits in PENDING until every injected service is usable, then loads. If
a provider unloads or is replaced, its dependents unload first, and load again
once a provider is back. A plugin whose `apply` raises is FAILED; everything it
registered is rolled back and no other plugin is affected.

`steps`, `workflows`, `doctor`, `cli` and `tasks` are *context-bound*: each plugin
receives a view made for it, which is how `ctx.steps.register(...)` records the
registration as an effect of the calling plugin.

The rest of the `Context` API:

| Member | Purpose |
| --- | --- |
| `ctx.provide(name, value)` | Provide a service while this plugin is loaded. A name already provided raises `ServiceConflict`. |
| `ctx.plugin(plugin, config=None, *, name=None)` | Load a child plugin, disposed together with this one. |
| `ctx.on(event, callback, *, prepend=False)` | Listen to an event (see [Events](#events)). |
| `ctx.emit` / `ctx.bail` / `ctx.waterfall` | Dispatch an event. |
| `ctx.effect(body, label="effect")` | Run `body` now and undo it on unload (see [Effects and cleanup](#effects-and-cleanup)). |
| `ctx.logger` | A logger named `runspool.plugin.<entry id>`. |
| `ctx.fiber` | The plugin's fiber: `name`, `state`, `error`. |

## Services

The services a profile with the default bundles provides:

| Service | What a plugin uses |
| --- | --- |
| `config` | The engine settings (`AppConfig`): `workspace_root`, `base_dir` (the profile's directory), `workflows`, `database_path`, ... Read-only by convention. |
| `startup` | `report()`: the startup report (`lines()` lists entries that are not active); `warnings`: profile warnings. |
| `store` | Persistence primitives: `repo`, `log`, `step_runs`, `check()`. Read freely; change task status only through `tasks`. |
| `credentials` | `resolve(name)`, `describe(name)`, `check(names, label="credentials")`. |
| `steps` | `register(step)`, `register_lazy(name, loader)`, `guard(check)`, `has(name)`, `names()`, `side_effect_of(name)`, `resolve_all()`. |
| `workflows` | `add_default(name, steps)`, `get(name)`, `names()`. |
| `tasks` | `add(input, *, workflow, name=None, force=False, metadata=None, parent=None)`, `get(id)`, `children(id)`, `pause`, `resume`, `terminate`, `retry`, `approve(id, *, by="")`, `reject(id, *, by="", reason="")`, `machine(workflow)`. |
| `runtime` | `run_until_idle()`, `ensure_ready()`, `build_daemon()`, `daemon_status()`. Mostly for the CLI and tests. |
| `doctor` | `register(check)`, `run()`. |
| `approval` | `policy` (`"ask"` or `"never"`), `requested(task)`. Optional: may be disabled. |
| `cli` | `register(command, *, name=None)`, `commands`. Optional: may be disabled. |

Register through the methods, not the `registry`, `guards` or `commands`
attributes, so the kernel can undo it.

## Registering steps

`ctx.steps.register(step)` takes a `Step` instance, or a zero-argument factory
that returns one. Steps never receive `ctx`; give a step what it needs through
its constructor, as the WeChat plugin does:

```python
def apply(ctx, config: WechatConfig) -> None:
    ctx.steps.register(_steps.WeChatRenderStep(config))
    ctx.steps.register(_steps.WeChatDraftStep(config, ctx.credentials))
```

Step names are global. Registering a name that is already taken raises, which
fails your plugin and leaves the first registration in place. To replace a
built-in step, disable its entry and register your own under the same name:

```yaml
patch:
  - id: builtin-archive
    disabled: true
  - insert:
      - {id: my-archive, plugin: "my_project.archive:plugin"}
```

`register_lazy(name, loader)` reserves the name now and calls `loader()` the first
time the step is needed, so read-only commands never import your step code:

```python
def _load_ocr():
    from my_plugin.ocr import OcrStep  # heavy imports happen here
    return OcrStep()

ctx.steps.register_lazy("ocr_page", _load_ocr)
```

The loaded object must be a `Step` whose `name` equals the reserved name. `run`,
`daemon` and `doctor` import every lazy step up front and report any that fail.

### Guards

`ctx.steps.guard(check)` adds a check that runs before every step. It receives
the same `GateRequest` as `step/pre-execute` listeners and returns a reason to
refuse, or `None`:

```python
def inside_inbox(request):
    if not str(request.task["input"]).startswith(str(config.inbox)):
        return f"input is outside {config.inbox}"

ctx.steps.guard(inside_inbox)
```

A guard can only refuse, never allow, so the order in which plugins register can
never turn a refusal into permission. A refused step is not run and the task goes
to `manual_required`. A guard that raises refuses too.

## Contributing workflows

```python
ctx.workflows.add_default("wechat_article", ["wechat_render", "wechat_draft"])
```

A default only applies when nothing defines that name yet. The profile's own
`workflows:` entry of the same name always wins, and so does an earlier plugin's
default. Make the name configurable (WeChat uses `config.workflow`) so a profile
can run two variants side by side.

## CLI commands

`runspool` boots the profile before it parses arguments, so plugin commands are
real subcommands. Register a function (one command) or a `typer.Typer` (a group):

```python
import typer

def apply(ctx, config) -> None:
    def children(task_id: int) -> None:
        """List the sub-tasks of a task."""
        for child in ctx.tasks.children(task_id):
            typer.echo(f"{child['id']}  {child['task_status']}  {child['input']}")

    ctx.cli.register(children)  # runspool children 3

plugin = Plugin(name="family", apply=apply, inject=["tasks", "cli"])
```

A function's command name is its `__name__` with `_` replaced by `-`; a group's
is `Typer(name=...)`; `name=` overrides either. The command closes over the
plugin's `ctx`, so it uses the plugin's services directly. The WeChat plugin
registers a `wechat` group with `token` and `preview` commands.

A plugin command cannot shadow a built-in one (`run`, `status`, `approve`, ...):
it is ignored with a warning. Two plugins registering the same name fail the
second plugin.

## Doctor checks

`ctx.doctor.register(check)` adds a check to `runspool doctor`. `check()` returns
a `Check` or a list of them; one that raises is reported as a failed check.

```python
from runspool.core.doctor import Check

ctx.doctor.register(lambda: Check("inbox", config.inbox.is_dir(), str(config.inbox)))
ctx.doctor.register(lambda: ctx.credentials.check(names, "wechat credentials"))
```

`credentials.check` is the usual way to report missing credentials: it names
what is missing and where the others come from, never their values.

## Events

Three dispatch modes:

| Mode | Semantics |
| --- | --- |
| `emit(event, *args)` | Broadcast. Return values are ignored; a listener that raises is logged and skipped, so observers cannot break the emitter. |
| `bail(event, *args)` | Listeners run in order; the first result that is not `None` or `False` wins and stops the chain. Exceptions propagate. |
| `waterfall(event, *args, default=...)` | Middleware: each listener is called as `listener(*args, next)` and decides whether to call `next()`. Not calling it vetoes the rest of the chain; `default()` is the innermost result. A listener may call `next()` at most once. |

Listeners run in registration order (mount order, roughly); `prepend=True`
puts a listener first. A listener is an effect of the plugin that registered it
and goes away when that plugin unloads.

The events RunSpool itself dispatches:

| Event | Mode | Arguments |
| --- | --- | --- |
| `step/pre-execute` | waterfall | `(request, next)`: decides whether a claimed step may run |
| `approval/request` | emit | `(task)`: a task has started waiting for approval |
| `internal/status` | emit | `(fiber)`: a plugin's lifecycle state changed (kernel internal) |

`step/pre-execute` and `approval/request` are dispatched on the worker thread
about to run the step. Keep listeners quick and thread-safe. Plugins may define
their own event names for each other; prefix them with your plugin's name.

### `step/pre-execute`

Before a claimed step runs, the runtime calls this waterfall with a
`GateRequest`:

| Field | Meaning |
| --- | --- |
| `request.task` | The task row, read-only |
| `request.step` | The `Step` about to run |
| `request.attempt` | Which run of this step this would be (1 the first time) |
| `request.granted` | Whether a human approved exactly this attempt |

A listener returns one decision from `runspool.engine.gate`, or `next()`:

| Decision | Effect on the task |
| --- | --- |
| `Allow()` | The step runs (subject to guards and the side-effect rule below). |
| `Deny(reason)` | Not run; `manual_required` with `last_error = "not run: <reason>"`. |
| `Defer(reason, delay_seconds=0)` | Not run; back to `queued` on the same step, not before the delay. |
| `Ask(reason)` | Needs approval; `awaiting_approval`, with `reason` shown to the approver. |

The default, when every listener passes, is `Allow()`. Anything that is not one
of these four, and any exception, refuses the step. A step held by `Deny`,
`Defer` or `Ask` records no step run, so `attempt` does not advance.

The WeChat plugin refuses a draft with problems before anyone is asked to
approve it, and tells the approver what would leave the machine:

```python
def gate_drafts(request, next_):
    if request.step.name != "wechat_draft":
        return next_()
    summary = _steps.rendered_summary(ctx.config, request.task)
    if summary is None:
        return next_()  # not rendered: the step itself reports it
    refusal = _steps.draft_refusal(summary, config)
    if refusal:
        return Deny(refusal)
    files = ", ".join(summary.get("uploads", [])) or "nothing"
    return Ask(f"save a WeChat draft of {summary['title']!r}, uploading: {files}")

ctx.on("step/pre-execute", gate_drafts)
```

After the listeners, RunSpool applies its own rules in this order: a `Deny` or
`Defer` stands; then guards may refuse; then a step with `side_effect = True`, or
an `Ask`, needs approval unless this attempt was already granted. No listener can
wave a step with side effects through: returning `Allow()` without calling
`next()` skips the listeners after it, not the approval. An `Ask` for an attempt
that is already granted lets the step run, so `gate_drafts` need not check
`request.granted`.

To wait for a time window or an outside condition, return
`Defer(reason, delay_seconds=...)` rather than refusing.

### `approval/request`

Emitted with the task (now `awaiting_approval`) when a step starts waiting. A
notification plugin listens to it:

```python
ctx.on("approval/request", lambda task: notify_owner(f"task {task['id']} awaits approval"))
```

## Side effects and approvals

Set `side_effect = True` on a step whose effects leave RunSpool: publishing,
uploading, sending a message, creating a draft on a platform (`WeChatDraftStep`
does). Whether a step has side effects is recorded when it is registered (for a
lazy step, the first time it is checked). Changing the attribute later does not
take a step out of approval.

Such a step runs only after a human approves that attempt. The task waits in
`awaiting_approval` without holding a worker; `runspool approve <id>` requeues it
with a grant for this step and this attempt, `runspool reject <id>` sends it to
`manual_required`. The grant covers one attempt, so a retry after a failure
(automatic or `runspool retry`), a re-run after a crash or reclaim, and a re-run
after the step raised `StepDeferred` all ask again. `runspool set-step` clears the
grant (and cannot be used while a task waits for approval). A pause requested
while the gate asks pauses the task in place; it asks again on resume. A gate
`Defer` records no run, so it does not use up a grant.

With approval policy `never` (`{id: approval, config: {policy: never}}`), or with
the `approval` entry disabled, such steps are refused instead of asked. Disabling
approval can make approval impossible, never unnecessary.

A plugin can approve or reject through `ctx.tasks.approve(task_id)` and
`ctx.tasks.reject(task_id, reason=...)`. The decision is recorded as
`plugin:<entry id>`, with any `by=` text appended in parentheses
(`approved by plugin:notify (wechat reply)`), whatever the plugin passes. Plugins
are trusted code: installing one that approves automatically hands it the
decision.

## Credentials

Configuration holds the *name* of a secret, never its value. Type such fields as
`CredentialRef`: a name in environment-variable style, upper case
(`^[A-Z_][A-Z0-9_]*$`). A lower-case string in that field is far more likely a
pasted secret, and validation rejects it without echoing it.

Inject `credentials`, hand it to the step, and resolve the name when you need the
value, as `WeChatDraftStep` does: `self._credentials.resolve(settings.appsecret)`.

| Method | Returns |
| --- | --- |
| `resolve(name)` | The value, or `None` if no source has a non-empty one |
| `describe(name)` | `CredentialInfo(name, configured, source)`, without the value |
| `check(names, label)` | A doctor `Check` naming what is missing, or where each name comes from |

Every lookup re-reads the sources, so a rotated secret needs no restart. Resolve
at the moment of use and do not keep the value. Never put a value in a log line,
an exception message, a `StepResult`, task metadata or an event; report the
*name* that is missing. See [architecture.md](architecture.md#credentials) for
where `credentials-local` looks.

## Effects and cleanup

Everything a plugin registers through `ctx` (steps, workflows, guards, listeners,
commands, doctor checks, provided services, child plugins) is an effect. When
the plugin unloads, its effects are undone in reverse order. If `apply` raises
partway, what it had registered is rolled back the same way.

For resources of your own, return a disposer from `apply`, or wrap a setup in
`ctx.effect`:

```python
def apply(ctx, config) -> None:
    def open_audit_log():
        handle = open(config.audit_file, "a", encoding="utf-8")
        return handle.close  # a disposer, or a list of them

    ctx.effect(open_audit_log, label="audit-log")

    pool = ThreadPoolExecutor(max_workers=2)
    return lambda: pool.shutdown(wait=True)
```

What `apply` returns (a callable, or an object with `dispose()`) runs last, after
every effect it registered. A disposer that raises is logged and teardown goes on.
Each registration method also returns a disposer, if you need to undo one early.
Registering on a plugin that is not loading or active raises
`InactiveEffectError`. Plugins unload when a service they inject changes, and when the kernel is
disposed; the daemon disposes it on exit so plugins can release threads, sockets
and files.

## Packaging

A plugin is an ordinary Python package. Declare it under the `runspool.plugins`
entry-point group, and optionally a bundle under `runspool.bundles`. From
`runspool-wechat`:

```toml
[project]
name = "runspool-wechat"
dependencies = ["runspool>=0.2,<0.3", "markdown-it-py>=3.0"]

[project.entry-points."runspool.plugins"]
wechat = "runspool_wechat:plugin"

[project.entry-points."runspool.bundles"]
wechat = "runspool_wechat:BUNDLE"
```

The requirement on `runspool` doubles as a compatibility declaration. Before
importing a plugin by entry-point name, the loader checks the running RunSpool
against it; an incompatible entry is skipped and the startup report says why. A
development or pre-release build counts as the release it leads to
(`0.2.0.dev3` satisfies `>=0.2`). A package without a `runspool` requirement is
accepted. To use an incompatible version anyway, exempt that exact version in the
profile: `allow: ["runspool-wechat@0.1.0"]`. This is a compatibility check, not a
security boundary.

A bundle is a list of patches (or a callable returning one) that says which
entries to mount and with what default config. WeChat's is
`BUNDLE = [{"insert": [{"id": "wechat", "plugin": "wechat"}]}]`.

An entry's `plugin` is an entry-point name or a `module:attr` reference. The
latter suits a plugin that lives next to a project's profile; it needs no
package, and no compatibility check applies to it. Installed bundles named
`core` or `builtin-steps` are ignored: those names are RunSpool's own.

## Mounting in a profile

The profile is the config file (`config.yaml`, or any path given with
`runspool -c`). Four top-level keys drive composition; every other key is an
engine setting.

```yaml
workspace_root: ./workspace
bundles: [core, builtin-steps, wechat]
required: [wechat]
patch:
  - id: wechat
    config:
      author: Your Name
  - insert:
      - {id: creator, plugin: example-creator}
```

| Key | Meaning |
| --- | --- |
| `bundles` | Bundles to apply, in order. Without it: `[core, builtin-steps]`. If you list bundles, list `core`. |
| `patch` | The profile's own patches, applied after every bundle. |
| `required` | Entry ids that must be active, or startup fails. |
| `allow` | Exact `name@version` exemptions from the compatibility check. |

Patches:

- `{insert: [entry, ...]}` appends entries `{id, plugin, config, disabled}`. A
  duplicate id is an error.
- `{id: x, ...}` modifies entry `x`. `disabled` replaces the flag (there is no
  "remove": disable instead). `config` is deep-merged: mappings merge key by key,
  anything else, lists included, replaces the old value. `config: null` is
  ignored with a warning. `plugin` asserts identity: if it differs, the patch is
  skipped with a warning. A patch for an unknown id is skipped with a warning.

So `{theme: {accent: red, font: serif}, tags: [a, b]}` patched with
`{theme: {accent: blue}, tags: [c]}` becomes `{theme: {accent: blue, font: serif}, tags: [c]}`.

Entries mount in order; a plugin whose services are not ready yet waits and
loads as soon as they are, so order rarely matters. The core entries `steps`,
`workflows`, `tasks`, `runtime` and `doctor` cannot be disabled. `run` and
`daemon` refuse to start while any enabled entry is not active; read-only
commands still work. `runspool doctor` lists what is pending and why. Disable an
entry you do not want rather than leaving it broken.

## Testing plugins

Boot a real profile in a temporary directory and drive tasks through it. From
the creator example's test:

```python
from runspool.app import load_context
from runspool.runtime import run_until_idle

def test_the_plugin_builds_a_draft_package(tmp_path):
    profile = tmp_path / "runspool.yaml"
    profile.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "patch:\n  - insert: [{id: creator, plugin: example-creator}]\n",
        encoding="utf-8",
    )
    ctx = load_context(profile)
    tid = ctx.service("tasks").add(str(MATERIALS), workflow="creator_publishing")
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(tid)["task_status"] == "completed"
```

- Mounting by entry-point name needs the package installed (editable is
  enough); otherwise mount it as `module:attr`.
- `ctx.service("startup").report().lines()` is empty when every entry is active.
- For credentials, `monkeypatch.setenv` the names and point `XDG_CONFIG_HOME` at
  `tmp_path`. Assert the secret appears in no event or step run.
- Test commands with `typer.testing.CliRunner().invoke(build_app(profile),
  ["-c", str(profile), ...])` (`build_app` is in `runspool.cli`).
- For approvals: run until `awaiting_approval`, call
  `ctx.service("tasks").approve(tid, by="test")`, run again.

A plugin that provides another `store` must pass the store contract:

```python
from runspool.testing import StoreContract

class TestMyStore(StoreContract):
    @classmethod
    def open_store(cls, location):
        return MyStore(location)
```

`open_store` must be a classmethod or staticmethod on an importable class: one
test opens the same store from other processes to check that exactly one claim
wins.

## Rules of thumb

- **Steps never get `ctx`.** A step receives a `StepContext` and returns a
  `StepResult`. Hand it services through its constructor.
- **Status changes only through the state machine.** Use `ctx.tasks` for
  lifecycle actions. `StepResult.updates` and `repo.update_fields` accept only
  `name`, `priority`, `max_retries`, `progress` and `metadata`.
- **Fail closed.** When unsure whether something may leave the machine, `Deny`
  or `Ask`; mark such steps `side_effect = True`.
- **Inject what you need, `ctx.get` what you can do without.**
- **Never log a secret.** Report credential names, not values.
- **Keep `apply` cheap.** Every command boots the profile; use `register_lazy` for
  steps with heavy imports.
