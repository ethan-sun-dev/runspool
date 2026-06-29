"""SQLite table definitions (idempotent creation)."""

SCHEMA = """
create table if not exists tasks (
    id integer primary key autoincrement,
    input text not null,
    name text,
    workflow text not null,
    step text not null,
    task_status text not null,
    priority integer not null default 0,
    retry_count integer not null default 0,
    max_retries integer not null default 3,
    locked_by text,
    locked_at text,
    heartbeat_at text,
    pause_requested integer not null default 0,
    terminate_requested integer not null default 0,
    last_error text,
    next_retry_at text,
    created_at text not null default (datetime('now')),
    updated_at text not null default (datetime('now')),
    progress text
);

create table if not exists task_events (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    event_type text not null,
    step text,
    message text,
    payload_json text,
    created_at text not null default (datetime('now'))
);

-- step_runs records the duration / outcome / error of each step execution.
create table if not exists step_runs (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    step text not null,
    status text not null,
    started_at text not null default (datetime('now')),
    finished_at text,
    duration_ms integer,
    error text
);

create index if not exists idx_tasks_status on tasks(task_status);
create index if not exists idx_events_task on task_events(task_id);
create index if not exists idx_runs_task on step_runs(task_id);
"""
