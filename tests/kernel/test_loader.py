"""Plugin resolution, compatibility gating, profiles and the startup audit."""

from __future__ import annotations

import sys
import textwrap
from types import SimpleNamespace

import pytest

from runspool.kernel import (
    Entry,
    Kernel,
    Loader,
    Plugin,
    Profile,
    StartupError,
    check_compat,
    compose_profile,
)


class FakeEntryPoint:
    """Stands in for importlib.metadata.EntryPoint (name, dist, load)."""

    def __init__(self, name, obj, *, dist_name=None, version="1.0", requires=()):
        self.name = name
        self._obj = obj
        self.dist = (
            None
            if dist_name is None
            else SimpleNamespace(
                metadata={"Name": dist_name}, version=version, requires=list(requires)
            )
        )

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def store_plugin(ctx, config):
    ctx.provide("store", "S")


def tasks_plugin():
    return Plugin(
        name="tasks", apply=lambda ctx, config: ctx.provide("tasks", ctx.store), inject=["store"]
    )


# -- compatibility ---------------------------------------------------------------


@pytest.mark.parametrize(
    "requires, runtime, allow, ok",
    [
        (["runspool>=0.2,<0.3"], "0.2.1", (), True),
        (["runspool>=0.2,<0.3"], "0.3.0", (), False),
        (["runspool>=0.2,<0.3"], "0.3.0", ("runspool-wechat@1.0",), True),
        (["runspool>=0.2"], "0.3.0a1", (), True),  # prereleases count
        (["httpx>=0.27"], "9.9.9", (), True),  # no runspool requirement: accepted
        (["runspool<0.2 ; extra == 'legacy'"], "0.2.0", (), True),  # extra-only requirement
        (None, "0.2.0", (), True),
    ],
)
def test_check_compat(requires, runtime, allow, ok):
    reason = check_compat("runspool-wechat", "1.0", requires, runtime, allow)
    assert (reason is None) is ok
    if not ok:
        assert "requires runspool" in reason


# -- mounting --------------------------------------------------------------------


def test_mount_resolves_entry_points_in_order_and_reports_active():
    kernel = Kernel()
    loader = Loader(
        kernel,
        plugins={
            "tasks": FakeEntryPoint("tasks", tasks_plugin()),
            "store-sqlite": FakeEntryPoint("store-sqlite", store_plugin),
        },
        runtime="0.2.0",
    )
    # tasks is mounted before its provider: still ends active.
    report = loader.mount([Entry("tasks", "tasks"), Entry("store", "store-sqlite")])
    assert [(s.id, s.state) for s in report.entries] == [("tasks", "active"), ("store", "active")]
    assert kernel.root.get("tasks") == "S"


def test_mount_resolves_module_attr_references(tmp_path, monkeypatch):
    (tmp_path / "local_steps.py").write_text(
        textwrap.dedent(
            """
            def apply(ctx, config):
                ctx.provide("greeting", config["text"])
            name = "local"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "local_steps", raising=False)
    kernel = Kernel()
    loader = Loader(kernel, plugins={}, runtime="0.2.0")
    report = loader.mount(
        [
            Entry("hello", "local_steps:apply", config={"text": "hi"}),
            Entry("bare", "local_steps"),  # a bare name means an entry point, not a module
        ]
    )
    assert kernel.root.get("greeting") == "hi"
    assert [(s.id, s.state) for s in report.entries] == [("hello", "active"), ("bare", "skipped")]


def test_disabled_entries_are_not_mounted():
    kernel = Kernel()
    loader = Loader(kernel, plugins={"p": FakeEntryPoint("p", store_plugin)}, runtime="0.2.0")
    report = loader.mount([Entry("store", "p", disabled=True)])
    assert report.entries == []
    assert kernel.fibers == []


def test_incompatible_and_broken_plugins_are_skipped_with_reasons():
    kernel = Kernel()
    loader = Loader(
        kernel,
        plugins={
            "old": FakeEntryPoint(
                "old", store_plugin, dist_name="runspool-old", requires=["runspool<0.2"]
            ),
            "broken": FakeEntryPoint("broken", ImportError("no module named lxml")),
        },
        runtime="0.2.0",
    )
    report = loader.mount([Entry("a", "old"), Entry("b", "broken"), Entry("c", "missing")])
    states = {s.id: (s.state, s.error) for s in report.entries}
    assert states["a"][0] == "skipped" and "requires runspool<0.2" in states["a"][1]
    assert states["b"][0] == "skipped" and "lxml" in states["b"][1]
    assert states["c"][0] == "skipped" and "no installed plugin" in states["c"][1]
    assert kernel.fibers == []


def test_allow_exempts_an_exact_version():
    kernel = Kernel()
    ep = FakeEntryPoint("old", store_plugin, dist_name="runspool-old", requires=["runspool<0.2"])
    report = Loader(kernel, plugins={"old": ep}, runtime="0.2.0", allow=["runspool-old@1.0"]).mount(
        [Entry("store", "old")]
    )
    assert report.entries[0].state == "active"


# -- audit -----------------------------------------------------------------------


def test_audit_names_missing_services_of_pending_entries():
    kernel = Kernel()
    loader = Loader(
        kernel, plugins={"tasks": FakeEntryPoint("tasks", tasks_plugin())}, runtime="0.2.0"
    )
    report = loader.mount([Entry("tasks", "tasks")])
    assert report.entries[0].state == "pending"
    assert report.entries[0].missing == ["store"]
    assert report.lines() == ["tasks: pending, waiting for store"]
    with pytest.raises(StartupError, match="waiting for store") as caught:
        loader.audit(required=["tasks"])
    assert caught.value.report.entries[0].id == "tasks"


def test_audit_ignores_optional_failures_but_not_required_ones():
    def broken(ctx, config):
        raise RuntimeError("needs a token")

    kernel = Kernel()
    loader = Loader(
        kernel,
        plugins={
            "store": FakeEntryPoint("store", store_plugin),
            "notify": FakeEntryPoint("notify", broken),
        },
        runtime="0.2.0",
    )
    loader.mount([Entry("store", "store"), Entry("notify", "notify")])
    report = loader.audit(required=["store"])  # optional failure tolerated
    assert [s.id for s in report.not_active()] == ["notify"]
    assert "needs a token" in report.lines()[0]
    with pytest.raises(StartupError, match="needs a token"):
        loader.audit(required=["store", "notify"])


def test_audit_flags_required_entries_that_were_never_mounted():
    loader = Loader(Kernel(), plugins={}, runtime="0.2.0")
    loader.mount([])
    with pytest.raises(StartupError, match="required but not mounted"):
        loader.audit(required=["store"])


# -- profiles --------------------------------------------------------------------


def test_profile_composes_bundles_then_profile_then_user_then_overlays(tmp_path):
    profile_path = tmp_path / "runspool.yaml"
    profile_path.write_text(
        textwrap.dedent(
            """
            bundles: [core, extras]
            required: [store]
            patch:
              - id: store
                config: {path: app.db}
            workspace_root: ./workspace
            """
        ),
        encoding="utf-8",
    )
    user = tmp_path / "user.yaml"
    user.write_text("- id: store\n  config: {wal: false}\n", encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("- id: notify\n  disabled: true\n", encoding="utf-8")
    bundles = {
        "core": FakeEntryPoint(
            "core",
            [{"insert": [{"id": "store", "plugin": "store-sqlite", "config": {"wal": True}}]}],
        ),
        "extras": FakeEntryPoint(
            "extras", lambda: [{"insert": [{"id": "notify", "plugin": "notify"}]}]
        ),
    }
    profile = Profile.load(profile_path)
    assert profile.required == ["store"]
    assert profile.settings == {"workspace_root": "./workspace"}
    result = compose_profile(profile, bundles=bundles, user_patch=user, overlays=[overlay])
    assert [(e.id, e.config, e.disabled) for e in result.entries] == [
        ("store", {"wal": False, "path": "app.db"}, False),
        ("notify", None, True),
    ]


def test_unknown_bundle_is_an_error(tmp_path):
    from runspool.kernel import ComposeError

    with pytest.raises(ComposeError, match="no installed bundle"):
        compose_profile(Profile(bundles=["nope"]), bundles={})
