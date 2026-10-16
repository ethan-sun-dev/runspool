-- A schema v5 database written by RunSpool at 8314554 (external steps, R-3):
-- `runspool init`, `add` + `run` of a file with a non-ASCII name (task 1, completed,
-- with one recorded artifact via ArtifactLog.record), then a task of a workflow
-- with one external step (task 2): `run` opened its work item, which was claimed
-- and submitted (task 2 queued, item submitted). Dumped with `sqlite3 .dump`; the
-- original directory is replaced by /home/user/demo.
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE schema_meta (key text primary key, value text not null);
INSERT INTO schema_meta VALUES('version','5');
CREATE TABLE tasks (
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
    progress text,
    claim_token text
, metadata text, parent_task_id integer references tasks(id), approval_grant text);
INSERT INTO tasks VALUES(1,'/home/user/demo/笔记 一.txt','笔记 一','local_file','archive','completed',0,0,3,NULL,NULL,NULL,0,0,NULL,NULL,'2026-10-16 03:25:22','2026-10-16 03:25:22',NULL,NULL,NULL,NULL,NULL);
INSERT INTO tasks VALUES(2,'审阅 稿件',NULL,'reviewed','review','queued',0,0,3,NULL,NULL,NULL,0,0,NULL,NULL,'2026-10-16 03:25:22','2026-10-16 03:25:22',NULL,NULL,NULL,NULL,NULL);
CREATE TABLE task_events (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    event_type text not null,
    step text,
    message text,
    payload_json text,
    created_at text not null default (datetime('now'))
);
INSERT INTO task_events VALUES(1,1,'created','ingest_file','task created',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(2,1,'claimed','ingest_file','claimed by pool-1',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(3,1,'step_completed','ingest_file','advanced to classify_text',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(4,1,'claimed','classify_text','claimed by pool-1',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(5,1,'step_completed','classify_text','advanced to normalize_markdown',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(6,1,'claimed','normalize_markdown','claimed by pool-1',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(7,1,'step_completed','normalize_markdown','advanced to summarize_text',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(8,1,'claimed','summarize_text','claimed by pool-1',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(9,1,'step_completed','summarize_text','advanced to archive',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(10,1,'claimed','archive','claimed by pool-1',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(11,1,'completed','archive','workflow completed',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(12,2,'created','review','task created',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(13,2,'claimed','review','claimed by pool-2',NULL,'2026-10-16 03:25:22');
INSERT INTO task_events VALUES(14,2,'item_opened','review','waiting for external work (attempt 1 of 3)','{"attempt": 1, "reused": false}','2026-10-16 03:25:22');
INSERT INTO task_events VALUES(15,2,'item_claimed','review','item 1','{"item_id": 1, "by": "人工"}','2026-10-16 03:25:22');
INSERT INTO task_events VALUES(16,2,'item_submitted','review','item 1 submitted by 人工','{"item_id": 1, "by": "人工"}','2026-10-16 03:25:22');
CREATE TABLE step_runs (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    step text not null,
    status text not null,
    started_at text not null default (datetime('now')),
    finished_at text,
    duration_ms integer,
    error text,
    note text
);
INSERT INTO step_runs VALUES(1,1,'ingest_file','ok','2026-10-16 03:25:22','2026-10-16 03:25:22',0,NULL,'ingested 笔记 一.txt (4 words)');
INSERT INTO step_runs VALUES(2,1,'classify_text','ok','2026-10-16 03:25:22','2026-10-16 03:25:22',0,NULL,'classified as general (confidence 0.0)');
INSERT INTO step_runs VALUES(3,1,'normalize_markdown','ok','2026-10-16 03:25:22','2026-10-16 03:25:22',0,NULL,'normalized to 3 lines');
INSERT INTO step_runs VALUES(4,1,'summarize_text','ok','2026-10-16 03:25:22','2026-10-16 03:25:22',0,NULL,'summarized 4 words');
INSERT INTO step_runs VALUES(5,1,'archive','ok','2026-10-16 03:25:22','2026-10-16 03:25:22',0,NULL,'archived to /home/user/demo/ws/ready/1');
CREATE TABLE artifacts (
            id integer primary key autoincrement,
            task_id integer not null references tasks(id),
            step text not null,
            step_run_id integer references step_runs(id),
            type text not null,
            path text not null,
            sha256 text not null,
            size_bytes integer,
            created_at text not null default (datetime('now'))
        );
INSERT INTO artifacts VALUES(1,1,'archive',5,'doc','ready/1/笔记 一.txt','abababababababababababababababababababababababababababababababab',42,'2026-10-16 03:25:22');
CREATE TABLE work_items (
            id integer primary key autoincrement,
            task_id integer not null references tasks(id),
            step text not null,
            attempt integer not null default 1,
            max_attempts integer not null,
            status text not null,
            kind text not null,
            instructions text not null default '',
            payload_json text,
            previous_error text,
            claimed_by text,
            claim_token text,
            claimed_at text,
            submission_json text,
            submitted_by text,
            submitted_at text,
            error text,
            step_run_id integer references step_runs(id),
            created_at text not null default (datetime('now')),
            updated_at text not null default (datetime('now'))
        );
INSERT INTO work_items VALUES(1,2,'review',1,3,'submitted','human','校对「笔记」','{"input": "审阅 稿件"}',NULL,'人工','5752c050b66e4e1c841afc0edde639a9','2026-10-16 03:25:22','{"value": "改好了"}','人工','2026-10-16 03:25:22',NULL,NULL,'2026-10-16 03:25:22','2026-10-16 03:25:22');
INSERT INTO sqlite_sequence VALUES('tasks',2);
INSERT INTO sqlite_sequence VALUES('task_events',16);
INSERT INTO sqlite_sequence VALUES('step_runs',5);
INSERT INTO sqlite_sequence VALUES('artifacts',1);
INSERT INTO sqlite_sequence VALUES('work_items',1);
CREATE INDEX idx_tasks_status on tasks(task_status);
CREATE INDEX idx_events_task on task_events(task_id);
CREATE INDEX idx_runs_task on step_runs(task_id);
CREATE INDEX idx_tasks_parent on tasks(parent_task_id);
CREATE INDEX idx_artifacts_task on artifacts(task_id);
CREATE INDEX idx_work_items_status on work_items(status);
CREATE INDEX idx_work_items_task on work_items(task_id);
CREATE UNIQUE INDEX idx_work_items_active on work_items(task_id) where status in ('open', 'claimed', 'submitted');
COMMIT;
