from pathlib import Path

import pytest

from runspool.config import AppConfig


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_defaults_resolve_under_workspace(tmp_path):
    cfg = AppConfig.load(_write(tmp_path, f"workspace_root: {tmp_path}/ws\n"))
    assert cfg.database_path == tmp_path / "ws" / "runspool.db"
    assert cfg.logs_dir == tmp_path / "ws" / "logs"
    # Default workflow is provided when none is configured.
    assert "local_file" in cfg.workflows


def test_workflow_lookup(tmp_path):
    cfg = AppConfig.load(
        _write(tmp_path, f"workspace_root: {tmp_path}\nworkflows:\n  w:\n    steps: [a, b]\n")
    )
    wf = cfg.workflow("w")
    assert wf.steps == ("a", "b")
    with pytest.raises(KeyError):
        cfg.workflow("missing")


def test_step_quota_default_and_override(tmp_path):
    cfg = AppConfig.load(
        _write(tmp_path, f"workspace_root: {tmp_path}\nconcurrency:\n  fast: 4\n")
    )
    assert cfg.step_quota("fast") == 4
    assert cfg.step_quota("other") == 1


def test_plugin_import_alias_and_base_dir(tmp_path):
    cfg = AppConfig.load(
        _write(
            tmp_path,
            f"workspace_root: {tmp_path}\n"
            "plugin_paths: [steps]\n"
            "steps:\n  custom:\n    import: 'mod:Cls'\n",
        )
    )
    assert cfg.steps["custom"].import_target == "mod:Cls"
    assert cfg.resolved_plugin_paths() == [tmp_path.resolve() / "steps"]
