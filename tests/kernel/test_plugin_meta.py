"""Plugin metadata: category, description, external dependencies, hosts and the
config model's JSON Schema; listed by ``Loader.describe`` and ``runspool plugins``,
and binaries checked by ``doctor``."""

from __future__ import annotations

import json
import textwrap

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import app
from runspool.kernel import (
    Entry,
    External,
    Kernel,
    Loader,
    Plugin,
    PluginMeta,
    PluginShapeError,
    config_schema,
)
from runspool.kernel.plugin import normalize
from tests.kernel.test_loader import FakeEntryPoint


class PublishConfig(BaseModel):
    account: str
    retries: int = 3


publisher = Plugin(
    name="publisher",
    apply=lambda ctx, config: None,
    Config=PublishConfig,
    category="publish",
    description="Publishes drafts",
    external=[
        External("binary", "ffmpeg", "brew install ffmpeg"),
        {"kind": "account", "name": "x"},
    ],
    hosts=["cli", "desktop"],
)


def test_a_plugin_declares_metadata():
    meta = normalize(publisher).meta
    assert meta == PluginMeta(
        category="publish",
        description="Publishes drafts",
        external=(External("binary", "ffmpeg", "brew install ffmpeg"), External("account", "x")),
        hosts=("cli", "desktop"),
    )


def test_class_and_function_plugins_declare_it_as_attributes():
    class Cls:
        category = "storage"
        hosts = ("cli",)

        def __init__(self, ctx, config):
            pass

    def fn(ctx, config):
        pass

    fn.description = "does a thing"
    assert normalize(Cls).meta == PluginMeta(category="storage", hosts=("cli",))
    assert normalize(fn).meta == PluginMeta(description="does a thing")


def test_a_plugin_without_metadata_has_empty_metadata():
    assert normalize(lambda ctx, config: None).meta == PluginMeta()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hosts": "cli"},
        {"category": 3},
        {"external": [{"kind": "binary"}]},
        {"external": ["ffmpeg"]},
        {"external": {"kind": "binary", "name": "ffmpeg"}},
    ],
)
def test_malformed_metadata_is_a_shape_error(kwargs):
    with pytest.raises(PluginShapeError):
        normalize(Plugin(name="p", apply=lambda ctx, config: None, **kwargs))


def test_config_schema():
    schema = config_schema(PublishConfig)
    assert schema["required"] == ["account"]
    assert schema["properties"]["retries"]["default"] == 3
    assert config_schema(None) is None
    assert config_schema(lambda raw: raw) is None  # not pydantic: no schema


def test_describe_lists_mounted_and_installed_plugins():
    applied = []
    eps = {
        "publisher": FakeEntryPoint("publisher", publisher, dist_name="acme-publish"),
        "spare": FakeEntryPoint(
            "spare", Plugin(name="spare", apply=lambda c, k: applied.append(k), category="extra")
        ),
        "broken": FakeEntryPoint("broken", ImportError("no such module")),
    }
    loader = Loader(Kernel(), plugins=eps, runtime="0.2.0")
    loader.mount([Entry(id="pub", plugin="publisher", config={"account": "a"})])

    (pub,) = loader.describe()
    assert (pub.id, pub.state) == ("pub", "active")
    assert (pub.distribution, pub.version) == ("acme-publish", "1.0")
    assert pub.meta.category == "publish"
    assert pub.config_schema["required"] == ["account"]

    infos = {i.plugin: i for i in loader.describe(installed=True)}
    assert set(infos) == {"publisher", "spare", "broken"}
    assert (infos["spare"].id, infos["spare"].state) == (None, "available")
    assert infos["spare"].meta.category == "extra"
    assert infos["broken"].state == "unavailable"
    assert "no such module" in infos["broken"].error
    assert infos["broken"].to_dict()["category"] is None
    assert applied == []  # listing installed plugins never applies them
    assert [i.id for i in loader.describe()] == ["pub"]


def _profile(tmp_path, monkeypatch, binary: str):
    module = tmp_path / "meta_plugins.py"
    module.write_text(
        textwrap.dedent(
            f"""
            from runspool.kernel import External, Plugin

            plugin = Plugin(
                name="needs-tool",
                apply=lambda ctx, config: None,
                category="media",
                description="Needs a program",
                external=[External("binary", {binary!r}, "install it")],
            )
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(__import__("sys").modules, "meta_plugins", raising=False)
    path = tmp_path / "runspool.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        'patch:\n  - insert:\n      - {id: tool, plugin: "meta_plugins:plugin"}\n',
        encoding="utf-8",
    )
    return path


def test_doctor_checks_declared_programs(tmp_path, monkeypatch):
    ctx = load_context(_profile(tmp_path, monkeypatch, "no-such-program-xyz"))
    check = {c.name: c for c in ctx.service("doctor").run()}["external"]
    assert not check.ok
    assert "no-such-program-xyz not on PATH, needed by tool (install it)" in check.detail


def test_doctor_passes_when_programs_are_found(tmp_path, monkeypatch):
    ctx = load_context(_profile(tmp_path, monkeypatch, "python3"))
    check = {c.name: c for c in ctx.service("doctor").run()}["external"]
    assert check.ok
    assert check.detail == "1 program(s) found"


def test_the_plugins_command(tmp_path, monkeypatch):
    path = _profile(tmp_path, monkeypatch, "python3")
    result = CliRunner().invoke(app, ["-c", str(path), "plugins", "--json"])
    assert result.exit_code == 0, result.output
    plugins = {p["id"]: p for p in json.loads(result.output)["plugins"]}
    tool = plugins["tool"]
    assert tool["state"] == "active"
    assert tool["category"] == "media"
    assert tool["external"] == [{"kind": "binary", "name": "python3", "hint": "install it"}]
    assert tool["config_schema"] is None
    assert "store" in plugins  # core entries are listed too

    result = CliRunner().invoke(app, ["-c", str(path), "plugins"])
    assert result.exit_code == 0, result.output
    assert any(
        line.startswith("tool") and "[media]" in line and "Needs a program" in line
        for line in result.output.splitlines()
    )
