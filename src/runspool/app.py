"""Application context: assembles config and persistence for the CLI and daemon."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runspool.config import AppConfig
from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import StateMachine
from runspool.persistence.step_run_log import StepRunLog

DEFAULT_CONFIG_FILENAME = "config.yaml"


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    repo: TaskRepository
    log: EventLog
    step_runs: StepRunLog

    def state_machine(self, workflow_name: str) -> StateMachine:
        return StateMachine(self.repo, self.log, workflow=self.config.workflow(workflow_name))


def load_context(config_path: Path | str) -> AppContext:
    config = AppConfig.load(Path(config_path))
    db = Database(config.database_path)
    db.init()
    return AppContext(
        config=config,
        db=db,
        repo=TaskRepository(db),
        log=EventLog(db),
        step_runs=StepRunLog(db),
    )


def config_template(workspace_root: Path | str) -> str:
    return f"""# Runspool configuration
# Local-first: all state lives under workspace_root. Nothing is sent anywhere.
workspace_root: {workspace_root}

scheduler:
  poll_interval_seconds: 5   # how often the daemon looks for work
  max_retries: 3             # default retry budget per task
  retry_delay_seconds: 60

worker_pool:
  size: 4                    # concurrent step executions
  heartbeat_timeout_seconds: 1800

# Per-step concurrency quota (defaults to 1). Raise it for cheap, parallelizable
# steps; keep it at 1 for steps that must not overlap.
concurrency: {{}}

# Workflows are ordered lists of step names. The built-in steps below need no
# network, API keys, or external tools.
workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]

# Load custom steps from your own code. See docs/writing-steps.md.
# plugin_paths: [steps]            # directories added to sys.path (relative to this file)
# steps:
#   my_custom_step:
#     import: "my_module:MyCustomStep"
"""


def init_app(config_path: Path | str, *, workspace_root: Path | str) -> bool:
    """Generate config (without overwriting an existing one) and initialise the
    database. Returns whether a new config file was created."""
    config_path = Path(config_path)
    created = False
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_template(workspace_root), encoding="utf-8")
        created = True
    load_context(config_path)  # initialises the database
    return created
