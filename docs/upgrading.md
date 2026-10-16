# Upgrading

## From 0.2.x to 0.3.0

0.3.0 adds external steps, child tasks, artifact contracts, an executor lock,
an event cursor, plugin metadata and keychain credentials. Every new feature
is opt-in. What can affect an existing setup is listed below, and in full under
"Breaking" in the [CHANGELOG](../CHANGELOG.md).

### Before you upgrade

1. **Stop every RunSpool process that uses the database**: the daemon
   (`runspool daemon-stop`), any `runspool run`, and any application that embeds
   RunSpool. A 0.2.x process does not take the executor lock, so 0.3.0 cannot
   tell that it is still running.
2. **Back up the database.** By default it is `runspool.db` in the
   `workspace_root` (the `database_path` setting moves it). With nothing
   running, copy it together with any `runspool.db-wal` and `runspool.db-shm`
   next to it, or use `sqlite3 runspool.db ".backup runspool-0.2.db"`. The
   backup is the only way back (see below).
3. **Check your plugins' RunSpool range.** RunSpool refuses to load a plugin
   whose declared range excludes the running version. A plugin that declares
   `runspool<0.3` is refused by 0.3.0. For the official WeChat plugin, use
   `runspool-wechat` 0.2.1 (0.2.0 declares `<0.3`). For other plugins, use a
   release that accepts 0.3, or exempt an exact version in the profile
   (`allow: [name@version]`) once you have checked it works.

### Install

```bash
uv tool install --force "runspool==0.3.0"
# with the WeChat plugin:
uv tool install --force "runspool==0.3.0" --with "runspool-wechat==0.2.1"
runspool --version
```

With pip: `pip install -U "runspool==0.3.0"` (and `"runspool-wechat==0.2.1"`).

### The database is upgraded automatically, and cannot go back

The first time a 0.3.0 command opens the database (any command, `runspool
status` included), it upgrades it in place to schema version 6. 0.2.x writes
version 3. From version 3 to 6 the upgrade only adds tables (`artifacts`,
`work_items`, `task_spawns`): existing tables, columns and rows are not
changed. A 0.1.x database has no version: it first goes through migrations 1
to 3, which add columns to its existing tables (`progress`, `name`,
`claim_token`, `metadata`, `parent_task_id` and `approval_grant` on `tasks`,
`note` on `step_runs`, each only if missing), and then gains the three
tables. The upgrade needs the executor lock:
if another 0.3.0 process is executing tasks, the command fails with
`ExecutorBusy` and leaves the database as it was.

There is no downgrade. Once upgraded, 0.2.x refuses to open the database
(`SchemaTooNew`). To go back to 0.2.x, reinstall it and restore the backup you
made before upgrading. Work done since the upgrade is in the upgraded copy
only.

### Plugin changes need a restart

Once `run` or the daemon has started, the set of plugins is fixed. Reloading,
loading or unloading a plugin, providing or withdrawing a service, and
registering or unregistering a step are all refused with
`PluginChangeRefused`. To apply a plugin change, edit the profile or install
the new plugin version, then restart the daemon (`runspool daemon-stop`, then
`runspool daemon`) or the next `run`. 0.2 did not support reloading plugins at
run time either; 0.3.0 enforces it. Code that embeds RunSpool can still load
plugins into a booted kernel before it starts the runtime, but not from inside
a plugin's `apply`.

### Other changes to check

- **One executor per database.** A second `run` or daemon on the same database
  exits with `ExecutorBusy`, naming the process that holds the lock. Running two
  executors side by side worked before; it no longer does (`run --force`
  included). Read-only commands (`status`, `inspect`, `logs`, ...) still work
  while a daemon runs.
- **`build_coordinator()`** (Python API) now refuses to start, with
  `StartupError`, when an enabled plugin failed to load, as `run` and the daemon
  already did.
- **New built-in commands**: `artifacts`, `items`, `events`, `plugins`. A
  plugin's own command with one of these names keeps working: it is used
  instead of the built-in one, which that profile then does not offer under its
  name (`--help` lists the plugin's command). Without such a plugin, the
  built-in command is there.
- **Scripts and agents** reading `--json` output may see the new status
  `awaiting_external` and new event types (`item_*`, `children_spawned`) once
  a workflow uses external steps or child tasks. `doctor` lists more checks
  (`external`, and `artifacts`, `plugin changes`, `credentials keychain` when
  they apply).
- **Artifact contracts** apply only to workflows that declare them; see
  [writing-steps.md](writing-steps.md). Nothing changes for workflows that
  declare nothing.
- **Custom stores** (a replacement for the `store` plugin): see below.

### Custom stores

A store that replaces the `store` plugin keeps working for what 0.2 did. Each
0.3.0 feature needs more from it, and fails with an error naming what is
missing when the store does not have it:

| Feature | What the store needs | Without it |
|---|---|---|
| Recorded artifacts | `store.artifacts`: `record(...)`, `list_for_task(task_id, ...)`, `latest(task_id)` (as `runspool.persistence.artifact_log.ArtifactLog`) | a step that returns `artifacts` fails; `runspool artifacts` lists nothing |
| External steps | `store.items` (as `runspool.persistence.work_items.WorkItems`: listing, `claim`, `release`, and the `opener` / `submitter` / `closer` / `canceller` hooks), and `repo.transition(..., also=hook)` running the hook in the transition's own transaction | an external step fails; `runspool items` cannot be used |
| Child tasks | `repo.spawner`, `repo.result_writer` and `repo.prepare_updates` (hooks run by `transition(..., also=)` in the same transaction), and `repo.list_children` to read them | a step that spawns fails ("the active store cannot spawn child tasks") |
| Event cursor | `store.log.since(cursor, *, task_id=None, limit=500)`, events with an id above `cursor`, oldest first | `runspool events` fails |

The executor lock is a file next to the configured `database_path`, whatever
the store keeps its data in.

This table is not a complete list of what a store must do. The conformance
suite, `runspool.testing.StoreContract`, is the reference: run it against your
store. In 0.3.0 the suite checks only the surface that 0.2 stores already had
(tasks, claims, transitions, events, step runs). It does not check the
interfaces above yet, so until it does, the SQLite implementation in
`runspool.persistence` is the reference for them.

### After upgrading

```bash
runspool --version     # 0.3.0
runspool doctor        # every check OK
runspool status
```

Then start the daemon again if you use one.

### Staying on 0.2.2

An installation pinned to `runspool==0.2.2` (with `runspool-wechat==0.2.0`, if
it uses the plugin) is not affected by the 0.3.0 release: nothing changes until
its pin changes. Keep its database separate: once a 0.3.0 process has opened a
database, 0.2.2 can no longer use it. Give each installation its own
`workspace_root` (or `database_path`), and never point both at the same file.
