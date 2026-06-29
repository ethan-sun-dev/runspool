"""Typed configuration: a pydantic schema loaded from YAML, with path resolution.

The config defines where state lives (``workspace_root`` and friends), how the
scheduler and worker pool behave, the named workflows (ordered step lists),
per-step concurrency quotas, and optional custom step plugins to load.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from runspool.models import WorkflowDef


class SchedulerConfig(BaseModel):
    poll_interval_seconds: int = Field(default=5, gt=0)
    max_retries: int = Field(default=3, ge=0)
    # 0 = retry immediately on the next tick. A positive value delays the retry
    # by that many seconds (backoff); timed retries are then driven by the daemon.
    retry_delay_seconds: int = Field(default=0, ge=0)


class WorkerPoolConfig(BaseModel):
    size: int = Field(default=4, gt=0)
    heartbeat_timeout_seconds: int = Field(default=1800, gt=0)


class WorkflowConfig(BaseModel):
    steps: list[str] = Field(min_length=1)


class StepPluginConfig(BaseModel):
    """A custom step to load dynamically.

    ``import`` is an ``"module.path:ClassName"`` target resolving to a ``Step``
    subclass with a no-argument constructor.
    """

    import_target: str = Field(alias="import")

    model_config = {"populate_by_name": True}


# Default workflow used when the config defines none. Uses only built-in steps so
# a fresh `runspool init` is runnable with zero extra setup.
_DEFAULT_WORKFLOWS: dict[str, list[str]] = {
    "local_file": [
        "ingest_file",
        "classify_text",
        "normalize_markdown",
        "summarize_text",
        "archive",
    ],
}


class AppConfig(BaseModel):
    workspace_root: Path
    database_path: Path | None = None
    logs_dir: Path | None = None
    runtime_dir: Path | None = None
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    worker_pool: WorkerPoolConfig = Field(default_factory=WorkerPoolConfig)
    workflows: dict[str, WorkflowConfig] = Field(default_factory=dict)
    concurrency: dict[str, int] = Field(default_factory=dict)
    # Directories prepended to sys.path before loading step plugins, resolved
    # under the config file's directory. Lets an example keep its custom steps
    # next to its config and load them regardless of the current directory.
    plugin_paths: list[str] = Field(default_factory=list)
    steps: dict[str, StepPluginConfig] = Field(default_factory=dict)

    # Directory of the config file (set by load()); used to resolve plugin_paths.
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _resolve_paths_and_defaults(self) -> AppConfig:
        root = self.workspace_root
        if self.database_path is None:
            self.database_path = root / "runspool.db"
        else:
            self.database_path = _resolve_under(root, self.database_path)
        if self.logs_dir is None:
            self.logs_dir = root / "logs"
        else:
            self.logs_dir = _resolve_under(root, self.logs_dir)
        if self.runtime_dir is None:
            self.runtime_dir = root / "runtime"
        else:
            self.runtime_dir = _resolve_under(root, self.runtime_dir)
        if not self.workflows:
            self.workflows = {
                name: WorkflowConfig(steps=steps) for name, steps in _DEFAULT_WORKFLOWS.items()
            }
        return self

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        config.base_dir = path.resolve().parent
        return config

    def workflow(self, name: str) -> WorkflowDef:
        if name not in self.workflows:
            raise KeyError(f"undefined workflow: {name!r}")
        return WorkflowDef(name=name, steps=self.workflows[name].steps)

    def step_quota(self, step: str) -> int:
        return self.concurrency.get(step, 1)

    def resolved_plugin_paths(self) -> list[Path]:
        """Plugin search directories as absolute paths."""
        base = self.base_dir or Path.cwd()
        out: list[Path] = []
        for p in self.plugin_paths:
            path = Path(p)
            out.append(path if path.is_absolute() else base / path)
        return out


def _resolve_under(root: Path, p: Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else root / p
