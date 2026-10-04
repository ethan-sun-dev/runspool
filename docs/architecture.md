# Architecture

RunSpool 0.2 is a small plugin kernel with everything else mounted into it as
plugins. This page describes how it is built: the layers, the kernel, the core
services, and how the reliability invariants from 0.1 are kept now that any of
it could be extended. [plugins.md](plugins.md) is the guide for plugin authors;
[concepts.md](concepts.md) explains tasks, workflows and steps.

## Layers

| Layer | What it is |
| --- | --- |
| Applications | A profile (`runspool.yaml` / `config.yaml`) plus, optionally, private plugin packages. An application is a composition, not a fork. |
| Official plugins | Separate packages: `runspool-wechat`, `runspool-example-creator`. |
| Built-in steps | One plugin per step (`builtin-ingest_file`, ..., `builtin-archive`), from the `builtin-steps` bundle. |
| Core plugins | `store`, `steps`, `workflows`, `tasks`, `runtime`, `doctor`, `credentials`, `approval`, `cli`, from the `core` bundle. |
| Kernel | `runspool.kernel`: plugins, services, effects, events, composition, loading. No knowledge of tasks, steps or storage. |

## The kernel

### Plugins and fibers

Each mounted plugin runs in a `Fiber`, which holds its context, validated config,
state and effects.

| State | Meaning |
| --- | --- |
| `pending` | Waiting until every injected service is usable. |
| `loading` | Config validated; `apply` is running. |
| `active` | Loaded. |
| `failed` | Config was invalid or `apply` raised. Its effects were rolled back; nothing else was touched. |
| `unloading` | Its effects are being disposed. |
| `disposed` | Removed for good, with its child plugins. |

Lifecycle changes are synchronous and single-threaded.

### Services and dependency activation

`ctx.provide(name, value)` registers a service. A name has one provider at a
time (`ServiceConflict` otherwise), and Context's own attribute names (`get`,
`on`, `emit`, ...) are reserved.

A service is *usable* by a consumer when its provider and every ancestor of the
provider are active. The exception: a plugin and its descendants may use what a
loading ancestor already provides. A provider whose `apply` is still running
never wakes outsiders early.

Each registration of a service gets a serial number. A fiber's *epoch* is the
list of `name=serial` pairs of the services it injects. Whenever a provided
service appears or disappears, the kernel reconciles the fibers that inject it:

- if the epoch changed, an active fiber unloads and loads again;
- a pending fiber whose services are all usable loads;
- a failed fiber is retried only against a different epoch, never the one it
  failed with.

Because the epoch counts registrations, not providers, a provider that reloads in
place also reloads its dependents. When a provider unloads, its dependents unload
first, while its services are still in place. Disposing a fiber mid-transition
(from its own `apply`, or from a disposer while it unloads) is deferred until the
transition ends.

`ctx.<name>` returns a service only if it is injected, or provided by the plugin
itself or a non-root ancestor; anything else raises `ServiceNotInjected`.
`ctx.get(name)` reads any usable service without declaring it. A service object
with a `for_context(ctx)` method is *context-bound*: each plugin receives
`service.for_context(its_ctx)`, which lets the service record a registration as
an effect of the calling plugin.

### Effects

Every registration (a provided service, a listener, a child plugin, a step, a
command) is an *effect*: a body that ran, plus the disposers that undo it. A
fiber unloads by running its effects in reverse order. Before calling `apply`,
the fiber reserves the first effect slot for what `apply` returns, so that
disposer runs last, after everything `apply` registered. If `apply` raises, the
same teardown rolls it back. A disposer that raises is logged and teardown
continues. Registering on a fiber that is not loading or active raises
`InactiveEffectError`.

### Events

| Mode | Semantics |
| --- | --- |
| `emit` | Broadcast; return values ignored; a listener that raises is logged and skipped. |
| `bail` | First result that is not `None` or `False` wins; exceptions propagate. |
| `waterfall` | Middleware `listener(*args, next)`; not calling `next()` vetoes the rest; `default()` is the innermost result; `next()` at most once per listener. |

Listeners are effects of the plugin that registered them. A listener removed
during a dispatch (its plugin disposed by an earlier listener) is skipped for
the rest of it. RunSpool dispatches `step/pre-execute` (waterfall),
`approval/request` (emit) and, from the kernel, `internal/status` (emit, with
the fiber whose state changed).

### Composition

An *entry* is one plugin to mount: `{id, plugin, config, disabled}`. A *layer* is
a list of patches applied over the entries built so far:

- `{insert: [...]}` appends entries; a duplicate id is an error;
- `{id: x, ...}` modifies entry `x`: `config` is deep-merged (mappings key by
  key; lists and scalars replace), `disabled` replaces the flag, `plugin` is an
  identity assertion. A patch for an unknown id or a mismatched `plugin` is
  skipped with a warning; `config: null` is ignored with one.

There is no remove; entries are disabled. `boot()` composes these layers, later
winning:

1. bundle layers, in profile order (`[core, builtin-steps]` if the profile lists
   none);
2. the `config-steps` entry, which registers the profile's 0.1-style `steps:` map
   lazily, after every bundle so built-in steps register first;
3. the profile's own `patch`;
4. overlay patch files passed to `boot(..., overlays=...)`.

The `core` and `builtin-steps` bundles ship inside RunSpool; an installed bundle
with either name is ignored with a warning. Composition warnings and unknown
profile settings are collected as startup warnings.

### Loader

The loader discovers plugins and bundles through the entry-point groups
`runspool.plugins` and `runspool.bundles`. An entry's `plugin` is either an
entry-point name or a `module:attr` reference.

For an entry-point name, the loader first checks the distribution's
`Requires-Dist` on `runspool` against the running version, using the base
release of a development build (`0.2.0.dev3` counts as `0.2.0`). A package with
no such requirement passes; so does any package when the runtime version is
unknown. An incompatible entry is skipped unless the profile's `allow` lists that
exact `name@version`. `module:attr` references are not checked. Import failures
and incompatibilities skip the entry with a reason; they never stop startup on
their own. Each enabled entry mounts under the root, named by its id.

### Startup and the audit

`boot()` runs these steps:

1. Load the profile. `bundles`, `required`, `allow` and `patch` drive
   composition; every other key is validated as `AppConfig`, with a relative
   `workspace_root` resolved against the profile's directory.
2. The root provides `config` and `startup`.
3. Compose the layers above.
4. Refuse to start if a locked core entry (`steps`, `workflows`, `tasks`,
   `runtime`, `doctor`) is disabled or missing.
5. Mount every enabled entry.
6. Audit: every locked core entry and every id in the profile's `required` must
   be active. Otherwise `StartupError` names each one and why: pending (and the
   services it waits for), failed (and the error), skipped (and the reason), or
   not mounted.
7. Check that something provides each of `store`, `credentials`, `steps`,
   `workflows`, `tasks`, `runtime` and `doctor`.

Without the audit, a plugin missing a provider would wait in `pending` silently.
Read-only commands work even if an optional plugin is not active. `run` and
`daemon` call `runtime.ensure_ready()`, which refuses to start while any enabled
entry is not active or any lazily registered step fails to import: a failed
plugin may have been meant to provide or override a step, and running without it
could run the wrong one. `runspool doctor` reports the same problems.

## Core services

Three roles:

- **core**: carries an invariant; one implementation; cannot be disabled.
- **seam**: fixed contract, replaceable implementation. Disable the entry and
  mount another plugin that provides the same service.
- **contribution**: what plugins register into core services: steps, default
  workflows, guards, listeners, commands, doctor checks. Each is an effect of
  the contributing plugin.

| Service | Entry (plugin) | Role | Responsibility |
| --- | --- | --- | --- |
| `config` | root | - | Engine settings (`AppConfig`) from the profile. |
| `startup` | root | - | Mount report and profile warnings. |
| `store` | `store` (`store-sqlite`) | seam, required | Tasks, events, step runs: create, atomic claim, compare-and-set transition, allow-listed field updates. Must pass `StoreContract`. |
| `credentials` | `credentials` (`credentials-local`) | seam, required | Secrets by name: `resolve`, `describe`, `check`. |
| `approval` | `approval` | seam, optional | Approval policy (`ask` / `never`) and the `approval/request` notification. |
| `steps` | `steps` | core | The step registry, side-effect flags, guards. |
| `workflows` | `workflows` | core | Workflow definitions from the profile, plus plugin defaults. |
| `tasks` | `tasks` | core | Task lifecycle through the state machine; records plugin approvals as `plugin:<id>`. |
| `runtime` | `runtime` | core | Runner, coordinator, worker pool, daemon, and the pre-execute gate. |
| `doctor` | `doctor` | core | Health checks: core checks plus registered ones. |
| `cli` | `cli` | optional | Collects plugin subcommands; `runspool` boots the profile before parsing arguments. |

## Reliability invariants

| # | Invariant | How it is enforced |
| --- | --- | --- |
| I1 | Task status is written only by the state machine | Compare-and-set transitions; `update_fields` allow-list |
| I2 | Steps never touch the database | Steps never receive `ctx`; `StepResult.updates` goes through the allow-list |
| I3 | A claimed step never runs twice | Atomic claim with a claim token, checked on every write |
| I4 | Pause and terminate apply at step boundaries; terminate wins | One boundary decision, `finish_step` |
| I5 | After a crash, an interrupted step is not done and re-runs cleanly | Recovery and reclaim settle tasks; grants are per attempt |
| I6 | Anything leaving the machine fails closed | The pre-execute gate refuses on every doubt |

**I1.** The store's `repo.transition(task_id, fields, expect=..., require=...,
events=...)` applies only if the task is still in an expected status and every
`require` column still holds the value read, and writes the transition's events
in the same transaction. The state machine reads, decides, and writes through it;
on contention it re-reads and decides again. `repo.update_fields` accepts only
`name`, `priority`, `max_retries`, `progress` and `metadata`. The lifecycle
columns (`workflow`, `step`, `task_status`, locks and heartbeat, `retry_count`,
the pause and terminate flags, `last_error`, `next_retry_at`, `claim_token`,
`approval_grant`) raise there. User actions (pause, retry, approve, ...) are
guarded in the state machine, so an illegal one raises `IllegalTransition`
whoever calls it.

**I2.** A step receives a `StepContext` (task, engine config, `should_stop`,
`heartbeat`, `attempt`) and returns a `StepResult`. It never sees a plugin
context; a plugin hands a step the services it needs through its constructor.
The runner persists `updates` through `update_fields`, so a step asking for a
lifecycle column fails instead of corrupting state.

**I3.** The coordinator claims with a single conditional update that succeeds only
while the task is `queued` and has no terminate request, recording a fresh
token. Before starting, the runner checks the task is still executing under that
token; a job that waited in a backlogged pool while its task was reclaimed does
nothing. Heartbeats, field updates and boundary transitions all require the
token. `StoreContract` checks exactly-one-winner claiming across threads and
across processes. `runspool run` refuses to run alongside a live daemon unless
forced.

**I4.** `should_stop()` signals termination only; pause is never signalled into a
step. Pause and terminate are flags, and the boundary decision below applies
them. A pause or terminate requested while the job waited for a worker is
honoured before the step starts.

**I5.** On daemon start and at the start of every `run`, every `running` or
`pause_pending` task is settled; each daemon tick also settles tasks whose
heartbeat is older than `heartbeat_timeout_seconds`, leaving a task alone if its
heartbeat moves meanwhile. Settling: a terminate request wins (`terminated`); a
pause request, or a task already `pause_pending`, pauses in place; anything else
is requeued on the same step. The interrupted step run is closed as
`interrupted`, and clearing the claim token turns the old worker's late finish
into a no-op. The re-run counts as a new attempt, so an approval does not carry
over.

**I6.** A step with side effects is refused when there is no gate or the gate
itself raises, when its side-effect flag cannot be determined, when a listener
raises or returns something that is not a decision, when a guard raises, when no
approval service is active, or when the policy is not `ask`. Guards can only
refuse.

## The step boundary

`StateMachine.finish_step(task_id, outcome, token=...)` decides every transition
at the end of a step run (or a gate refusal) in one place. It reads the flags and
writes the result as a compare-and-set that also requires the flags, the retry
counters and the claim token to be unchanged. A task that is no longer executing,
or is held by another claim, is left alone.

| Outcome | Terminate requested | Pause requested | Neither |
| --- | --- | --- | --- |
| Succeeded | `terminated` | advance, then `paused` on the next step (or finish) | `queued` on the next step, or `completed` / `partially_completed` after the last |
| Deferred (step or gate) | `terminated` | `paused` in place | `queued` on the same step, not before `next_retry_at` |
| Failed, retries left | `terminated` | failure recorded, `paused` in place | `failed`; requeued at `next_retry_at` |
| Failed, retries exhausted | `terminated` | `manual_required` | `manual_required` |
| Denied (gate) | `terminated` | `manual_required` | `manual_required` |
| Needs approval (gate) | `terminated` | `paused` in place | `awaiting_approval` |

A workflow ends `partially_completed` rather than `completed` when the latest run
of any step reported `degraded`.

## The approval gate

Before a claimed step runs, the runtime's gate decides, in this order:

1. **Snapshot.** Take what decides approval before any plugin runs: whether the
   step has side effects (recorded when it was registered; for a lazy step, the
   first time it is checked) and whether this attempt was granted (from the task
   as stored). Listeners receive a read-only copy of the task.
2. **Waterfall.** Plugins' `step/pre-execute` listeners decide; the default is
   `Allow`. `Deny` and `Defer` stand. Anything that is not a decision, or an
   exception, is a `Deny`.
3. **Guards.** Each `steps.guard` check may refuse, before anyone is asked to
   approve a step that would be refused anyway.
4. **Approval rule.** A step with side effects, or an `Ask`, needs approval
   unless this attempt was granted. With no active approval service, or a policy
   other than `ask`, it is refused instead.

Step 4 is RunSpool's own rule, applied after the plugins; disabling the
`approval` plugin makes such steps impossible to approve, never unnecessary. The
runner turns the decision into a boundary outcome: `Defer` into Deferred,
`Ask` into Needs approval, `Deny` into Denied. None of them records a step run, so
the attempt number, which is the count of recorded runs of that step plus one,
does not advance. Waiting stores `asked:<step>:<attempt>` in the task's
`approval_grant` and writes an `approval_asked` event; `approve` replaces it with
`granted:<step>:<attempt>` and requeues, `reject` sends the task to
`manual_required`; both write `approval_decided`. A retry, a crash re-run or a
re-run after `StepDeferred` is a new attempt and asks again; `set-step` clears the
grant. No worker is held while a task waits.

## Credentials

Configuration holds credential names (`CredentialRef`, `^[A-Z_][A-Z0-9_]*$`),
never values. `credentials-local` looks a name up in order:

1. the process environment;
2. the user's credentials file, `$XDG_CONFIG_HOME/runspool/credentials.yaml`
   (default `~/.config/runspool/credentials.yaml`), a `NAME: value` mapping;
3. a `.env` file next to the profile;
4. `~/.env`.

An empty value counts as absent. Every lookup re-reads the sources, so rotating a
secret needs no restart. Its config keys are `user_file` (relative paths resolve
against the profile), `profile_dotenv` and `home_dotenv`. A doctor check flags a
credentials file readable by other users, and a source that cannot be read is
reported without its content. `describe` and `check` report whether and where a
name is configured, never the value. `credentials` is a seam: a keychain or
vault plugin can replace it by providing the same methods.

## Schema migrations

The SQLite store records its schema version in `schema_meta` and applies
versioned migrations on open:

| Version | Change |
| --- | --- |
| 1 | Baseline tables; brings pre-versioning (0.1) databases to the same columns |
| 2 | `tasks.metadata`, `tasks.parent_task_id` |
| 3 | `tasks.approval_grant` |

The whole upgrade runs in one `BEGIN IMMEDIATE` transaction, so concurrent
openers (a daemon and a CLI) take turns, each re-reading the version inside it.
Every migration is idempotent; an interrupted upgrade leaves nothing half-applied.
A database whose version is newer than this RunSpool supports is refused
(`SchemaTooNew`) rather than written with an older layout. Connections use WAL
and a busy timeout.

## Not in 0.2

- **Hot reload.** The dependency mechanism can reload plugins, but in 0.2 the
  plugin set changes only at startup and shutdown.
- **Isolated service instances.** One provider per service name per process; to
  run two configurations, run two processes.
- **An async kernel.** The kernel is synchronous. Steps, and the gate in front of
  them, run on worker threads.
- **Sandboxing.** Plugins are trusted Python code. The compatibility check is not
  a security boundary, and a plugin that writes to the store directly or approves
  tasks itself is not stopped.
- Distribution across machines, a web UI, multi-tenancy.

## Attribution

The kernel adapts designs from two MIT-licensed projects. From **Cordis**
(Copyright (c) 2021-present Shigma): the fiber lifecycle, epoch-based dependency
activation, reversible effects, the event dispatch modes, and patch layers. From
**DeepSeek Harness** (Copyright (c) 2026 DeepSeek): the bundle and profile layers,
plugin compatibility gating, the startup audit, the approval pipeline with
refuse-only guards, and the ideas of a credentials service and of conformance
tests for seams. The modules that adapt code say so in their headers; the
licenses are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
