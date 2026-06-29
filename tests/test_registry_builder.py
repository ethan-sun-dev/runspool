"""Dynamic step loading: a custom step defined in a temp module is loaded via
plugin_paths + the steps: import map."""

import pytest

from runspool.app import load_context
from runspool.registry_builder import StepLoadError, build_registry, load_step

_PLUGIN_SOURCE = '''
from runspool.engine.step import Step, StepContext, StepResult


class GreetStep(Step):
    name = "greet"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(message="hello")


class MisnamedStep(Step):
    name = "actually_other"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult()
'''


def _write_plugin(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "myplugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    return plugins


def _config(tmp_path, steps_block):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "plugin_paths: [plugins]\n"
        "workflows:\n  w:\n    steps: [greet]\n"
        f"{steps_block}",
        encoding="utf-8",
    )
    return cfg


def test_build_registry_loads_plugin(tmp_path):
    _write_plugin(tmp_path)
    cfg = _config(tmp_path, "steps:\n  greet:\n    import: 'myplugin:GreetStep'\n")
    ctx = load_context(cfg)
    reg = build_registry(ctx.config)
    assert reg.has("greet")
    assert reg.get("greet").run(None).message == "hello"
    # built-ins still present
    assert reg.has("ingest_file")


def test_plugin_key_must_match_step_name(tmp_path):
    _write_plugin(tmp_path)
    cfg = _config(tmp_path, "steps:\n  greet:\n    import: 'myplugin:MisnamedStep'\n")
    ctx = load_context(cfg)
    with pytest.raises(StepLoadError):
        build_registry(ctx.config)


def test_load_step_bad_target():
    with pytest.raises(StepLoadError):
        load_step("no_colon_here")


def test_load_step_missing_attr():
    with pytest.raises(StepLoadError):
        load_step("runspool.engine.step:DoesNotExist")
