# Design decisions

Why Runspool is built the way it is. [concepts.md](concepts.md) explains *how*
the pieces work; this document records the *why* — the trade-offs taken and the
alternatives deliberately rejected. Each decision is small; together they're what
make a few thousand lines behave predictably under crashes and concurrency.

## 1. Local-first, single SQLite file — no server

**Decision.** All state lives in one SQLite database under `workspace_root`;
artifacts live next to it on the filesystem. There is no hosted service, no
broker, no daemon you must keep alive to inspect state.

**Why.** The target user is one person (or one agent) automating their own
machine. A Postgres/Redis-backed queue would add an operational surface — a
server to run, migrate, secure, and back up — that dwarfs the tool itself.
SQLite gives ACID transactions, durability across reboots, and a single file you
can copy, diff, or delete, with zero setup. The whole engine is importable and
testable in-process; every test in [`tests/`](../tests/) runs against a real
database in a temp dir, no fixtures-as-mocks.

**Trade-off.** No multi-machine distribution and a single-writer ceiling. That's
an explicit non-goal — Runspool is not a distributed scheduler. SQLite in WAL
mode (see below) comfortably handles a personal workload's concurrency.

## 2. The database stores lifecycle; the filesystem stores artifacts

**Decision.** Tasks, events, and step-run timing rows go in SQLite. The *outputs*
of work — normalized Markdown, summaries, JSON — are plain files under
`workspace_root/tasks/<id>/`, archived to `ready/<id>/` on completion.

**Why.** These have different access patterns and lifetimes. Lifecycle data is
small, queried by status, and needs transactional integrity. Artifacts are
arbitrary, sometimes large, and most useful as ordinary files a human or another
program can open directly. Keeping blobs out of the database keeps it small and
fast, and makes results inspectable without the tool (`cat ready/1/summary.md`).

**Trade-off.** State spans two stores, so they can drift if a process dies
between writing a file and committing a row. Recovery is built for this: an
interrupted step is re-run from a clean state rather than trusted to be
half-done (see §6).

## 3. Steps never touch the database

**Decision.** A step implements one method, `run(ctx) -> StepResult`. It reads
its input from `ctx`, does work, and returns data. It does **not** open the
database, change task status, or schedule retries. The
[`TaskRunner`](../src/runspool/engine/runner.py) persists the returned updates;
the [`StateMachine`](../src/runspool/persistence/state_machine.py) decides the
transition.

**Why.** This is the single most important boundary in the codebase. It means a
step author — the person most likely to be a newcomer or an AI agent — cannot
corrupt task state, deadlock the queue, or violate an invariant. Steps become
pure-ish, trivially unit-testable functions; persistence and transition logic
live in one place and are tested once. Compare the plugin contract in
[writing-steps.md](writing-steps.md): the surface a contributor must understand
is just `StepContext` in, `StepResult` out.

**Trade-off.** A step that genuinely needs to persist intermediate data must
route it through `StepResult.updates` (validated against a column allowlist —
see §12) or its own artifacts rather than writing directly. That indirection is
the point. Since 0.2 it is also structural: a step is never handed a plugin
context, so it has no way to reach the store or the state machine; what it needs
(a credential, an API client) is given to it when its plugin constructs it.

## 4. All transitions live in one state machine

**Decision.** Every status change — claim, complete, defer, fail, pause,
terminate, retry, recover — is a method on `StateMachine`. Guards (e.g. "you can
only resume a paused task") raise `IllegalTransition` from there, not from the
CLI.

**Why.** Lifecycle bugs are the expensive kind, because they corrupt state
silently and surface much later. Centralizing transitions means the legal state
graph is readable in one file (see the module docstring), and a guard can't be
forgotten in one of several call sites. The CLI, the coordinator, and recovery
all go through the same methods.

**Illustration.** This boundary is exactly what made one real bug a one-line
fix. `terminate` on a task that was mid-pause (`pause_pending`, so a worker is
still finishing its step) must win over the pending pause. Because both the
terminate path and the pause-at-boundary path are methods on the same machine,
ordering the precedence correctly — terminate is checked before pause at the
step boundary, and `request_terminate` defers to the flag instead of writing a
terminal status a finishing step could clobber — was localized and unit-testable
(`test_terminate_while_pause_pending_does_not_resurrect_as_paused`).

## 5. Claiming is atomic — conditional UPDATE, not check-then-act

**Decision.** A queued task is claimed with a single statement —
`UPDATE tasks SET task_status='running', locked_by=? WHERE id=? AND task_status='queued'`
— and the caller reads `cursor.rowcount` to learn whether it won
([`repository.claim_queued`](../src/runspool/persistence/repository.py)).

**Why.** The naive approach — read the row, see it's queued, then update — has a
race: two callers both read "queued" and both claim. That double-executes a
step, which for a side-effectful step (write a file, send a draft) is a real
fault. Pushing the predicate into the `WHERE` clause makes the database the
arbiter: exactly one `UPDATE` matches. This is what lets a one-shot `run` and a
live `daemon` coexist without a lock file around the whole engine.

Each claim also records a fresh **claim token**. The runner checks it before
starting the step and every transition the worker makes requires it, so a job
that sat in a backlogged pool while its task was reclaimed and claimed again
changes nothing.

**Trade-off.** It relies on the database's row-level atomicity rather than an
application mutex — which is precisely why it's correct across processes, not
just threads.

## 6. Pause and terminate apply at step boundaries, never mid-step

**Decision.** A running step is always allowed to finish. Pause and terminate set
a flag; the runner acts on it only after the current step returns. Pause then
*advances to the next step* before pausing, so the completed step is not re-run
on resume.

**Why.** Interrupting a step mid-execution would leave its side effects half-done
with no general way to undo them. Letting the step finish and acting at the
boundary keeps every step's effects all-or-nothing from the engine's point of
view. Crash recovery uses the same principle in reverse: a step interrupted by a
crash (not a clean boundary) is assumed *not* done and re-run from a clean state,
which is why steps should be written to be idempotent.

**Trade-off.** Terminate isn't instantaneous — a long step runs to completion
first. For a personal automation engine that's the right call; hard-killing
work mid-flight is the surprising behavior, not the safe one. (How every
combination of step outcome and request is resolved is §13.)

## 7. Heartbeats + reclaim instead of trusting workers

**Decision.** A running step's task carries a `heartbeat_at` the runner refreshes
on a timer (independently of whether the step cooperatively reports progress). A
task whose heartbeat goes stale past a timeout is reclaimed: requeued on the
same step, or — if a terminate or pause was requested meanwhile — terminated or
paused, exactly as crash recovery does.

**Why.** Workers crash, hang, or get `SIGKILL`ed. Without a liveness signal a
task would sit in `running` forever, invisible to the queue. A timer-based
heartbeat means even a step that never calls `ctx.heartbeat()` stays alive while
healthy and is reclaimed when truly dead — recovery doesn't depend on step
authors remembering to do anything.

**Trade-off.** A pathologically long but healthy step could be reclaimed if the
timeout is set too low; the default heartbeat interval is a third of the reclaim
timeout to give wide margin, and the timeout is configurable.

## 8. Retries are observable, not silent

**Decision.** On failure, `fail()` increments `retry_count`; while retries remain
the task goes to `failed` with a scheduled `next_retry_at` (the coordinator
requeues it when due); once exhausted it goes to `manual_required`, never silently
dropped. A human can raise the cap with `set-retries` to release a stuck task.

**Why.** A queue that silently retries forever hides real problems; one that
silently gives up loses work. Making both the retrying state and the
exhausted state first-class and visible (with the last error recorded) means the
operator always knows where a task stands. `manual_required` is a deliberate
"needs a human", not a dead end.

## 9. A JSON contract built for agents, not just humans

**Decision.** Every read command takes `--json`, and `inspect --json` returns not
only state but `available_actions` and a `suggested_next_action`.

**Why.** A primary consumer is an AI agent or a shell script, which needs to
*decide what to do next*, not parse a table. Returning the legal actions and a
suggestion turns the engine into something an agent can drive in a loop without
hard-coding the state graph. Consistency matters here too: a missing task returns
`{"error": "not_found"}` with a non-zero exit from `status`, `inspect`, **and**
`logs` — an empty list never stands in for "doesn't exist", which an agent would
misread. See [agent-json-output.md](agent-json-output.md) and the committed
[`sample-output/`](../sample-output/).

## 10. `run` and `daemon` are mutually exclusive by default

**Decision.** `run` (advance everything once, then exit) refuses to start while a
`daemon` is live, unless `--force`.

**Why.** `run` begins by recovering interrupted `running` tasks back to `queued`
— correct when nothing else is executing, catastrophic if a daemon is mid-step,
because it would yank the daemon's in-flight task and let the step run twice. The
guard makes the safe path the default and reserves the foot-gun for a deliberate
`--force`.

## 11. Everything is a plugin; the kernel knows nothing about tasks

**Decision.** Since 0.2 the core of Runspool is a small kernel that only knows
plugins, services, events and their lifecycle. The store, the step registry,
workflows, the task lifecycle, the runtime, the doctor, credentials, approval
and the CLI's extension point are all plugins, mounted from bundles and patched
by the profile — the same mechanism a third-party package uses. There is no back
door for built-ins.

**Why.** Real applications built on Runspool kept needing to swap one piece
(their own `archive`, a different store, extra commands) without forking the
engine. If built-ins used a private path, every such need would become a special
case in core. Making the engine itself the first user of the plugin API keeps
that API honest, and makes an application just a profile plus its own plugins.
Registrations are effects of the plugin that made them, undone when it unloads,
and a plugin waits until every service it declared is available — so ordering
and cleanup are the kernel's job, not each author's.

**Trade-off.** More indirection than 0.1's direct wiring, and startup has new
failure modes (a plugin missing, misconfigured, or incompatible). Those are made
loud rather than silent: a startup audit names every inactive entry and why,
`required` entries fail startup, `run` and `daemon` refuse a partially loaded set
(a failed plugin might have been meant to override a step), and the core entries
that carry the invariants below cannot be disabled. Plugins are trusted Python
code: version gating is a compatibility check, not a sandbox.

## 12. Status is written only by the state machine, by compare-and-set

**Decision.** The store exposes one way to change a task's lifecycle columns —
status, step, retry bookkeeping, locks, flags, approval — a compare-and-set
`transition` that applies only if the task is still in the status (and, where
needed, the flag values and claim token) the caller read, and that records its
events in the same transaction. Only the state machine calls it. The general
`update_fields` is allow-listed to `name`, `priority`, `max_retries`, `progress`
and `metadata`; a `StepResult.updates` naming anything else fails the step.

**Why.** Once the store is a seam any plugin can reach, "only the state machine
decides status" has to be enforced by the store's API rather than by
convention: with a general field update, a step or plugin could move a task
behind the machine's back. Compare-and-set closes the other gap — two writers
(a worker finishing, a user terminating, recovery) racing on the same task. The
loser's write simply does not apply; the machine re-reads and decides again
from the real state, instead of overwriting it.

**Trade-off.** Every transition is a read-decide-write loop with retries, and a
new lifecycle field means a new decision in the state machine rather than a
one-line update. That cost is paid once, in one module.

## 13. One place decides the step boundary — and terminate wins

**Decision.** When a step ends — returned, degraded, deferred, raised, refused by
the gate, held for approval, or never started because a request arrived while
it waited in the pool — the runner reports the outcome to a single method,
`finish_step`, which reads the pause and terminate flags and decides the
transition. Terminate wins over everything. A pause advances past a finished
step, pauses in place after a deferral, and after a failure records it and
pauses instead of scheduling a retry. Reclaim and crash recovery share one
equivalent "settle" rule.

**Why.** In 0.1 these combinations were decided along several code paths, and
each new outcome (deferral delays, degraded runs, approvals) multiplied them.
Precedence bugs between them — a terminate lost to a pause, a pause overwritten
by a retry, a stale worker resurrecting a reclaimed task — are exactly the
silent lifecycle bugs §4 warns about. With one decision table, every
combination has a written answer and a test, and the write that applies it is a
compare-and-set that also requires the flags, retry counters and claim token to
be unchanged, so a request landing mid-decision is never lost.

**Trade-off.** `finish_step` is the densest function in the codebase. It is
deliberately so: complexity concentrated where it can be read and tested beats
the same complexity spread thin.

## 14. Approval fails closed, and the runtime enforces it

**Decision.** A step marks itself `side_effect = True` when its effects leave
Runspool. Before any step runs, the runtime's gate asks plugins' policies first,
then applies Runspool's own rule last: a side-effect step runs only if a human
approved this exact attempt. The `approval` plugin only supplies the policy
(`ask` or `never`) and the notification; with it missing, set to `never`, or any
policy or guard erroring, the step is refused. Guards can only refuse. What
decides approval — the step's flag as recorded at registration, the grant as
stored — is captured before any plugin runs. A grant covers one attempt: a
retry, a re-run after a deferral or a crash asks again.

**Why.** Publishing, uploading or sending can't be undone, and the cost of an
unwanted one is far higher than the cost of a task waiting for a human. If the
rule lived in a replaceable plugin, disabling or replacing that plugin — or one
listener returning "allow" — would quietly turn approval off. Putting the rule
in the runtime means plugins can make approval stricter or impossible, never
unnecessary. Waiting happens in a task status, `awaiting_approval`, not in a
blocked worker, so an approval that takes a day costs nothing.

**Trade-off.** More prompts: a retry of an approved step asks again, even when
the human would say yes. That is the conservative reading of "approve this":
the earlier attempt may have done part of its work. Plugins are still trusted
code — one that approves tasks itself is not stopped, but its approvals are
recorded under its own name.

## 15. Credentials by name, never in config

**Decision.** Configuration holds only a secret's *name* (upper-case,
environment-variable style); a plugin resolves it through the `credentials`
service each time it needs it. The default provider reads the environment, an
owner-only credentials file, a `.env` next to the profile, then `~/.env`. The
service can describe whether a name is configured and where, but that
description never carries the value.

**Why.** Config files get committed, pasted into issues and printed by doctor;
secrets must not travel with them. Names make a profile shareable and let doctor
say exactly what's missing without leaking anything. Re-reading on every lookup
means rotation needs no restart. The error paths matter as much as the happy
path: a source that fails to parse is reported by file and line only, and plugin
config errors name fields without echoing input, because a parser error that
quotes the offending line can be a secret in a log.

**Trade-off.** One more indirection for plugin authors, and secrets live outside
the profile, so moving a setup to another machine means moving them separately.
The credentials service is a seam: a keychain or vault provider can replace it.

## 16. Versioned schema migrations

**Decision.** The database records its schema version. Opening it applies, in
order, the migrations it has not seen — each idempotent, all inside one
`BEGIN IMMEDIATE` transaction that re-reads the version. A database written by a
newer Runspool is refused rather than written with an older layout.

**Why.** 0.2 adds columns (metadata, parent, claim tokens, approval grants), and
there will be more. Ad-hoc "add the column if missing" checks don't say what
state a database is in and can't refuse one that's too new. A version number
does both, and taking the write lock before re-reading it means several
processes opening an old database at once (a daemon and a few CLI calls) upgrade
it exactly once. A 0.1 database is simply version 0 and upgrades in place.

**Trade-off.** Downgrading Runspool after a migration is not supported; the
newer database is refused, on purpose.

---

These choices share a theme: **make the safe behavior the default, push
correctness into the lowest layer that can enforce it (the database, the state
machine), and keep the surface a contributor or agent touches as small and as
hard to misuse as possible.**
