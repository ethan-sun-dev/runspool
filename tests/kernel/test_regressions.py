"""Regression tests for kernel bugs found in review of the first M1 commit."""

from __future__ import annotations

import sys
import textwrap

import pytest

from runspool.kernel import (
    Entry,
    FiberState,
    InactiveEffectError,
    Kernel,
    KernelError,
    Loader,
    Plugin,
    Profile,
    ServiceNotInjected,
    apply_patches,
    check_compat,
)


class Svc:
    def __init__(self):
        self.closed = False


def svc_provider(name="x", record=None):
    def apply(ctx, config):
        svc = Svc()
        if record is not None:
            record.append(svc)
        ctx.provide(name, svc)
        return lambda: setattr(svc, "closed", True)

    return Plugin(name=f"provide-{name}", apply=apply)


# 1 -------------------------------------------------------------------------------
def test_dispose_reentered_from_own_teardown_is_idempotent():
    kernel = Kernel()
    holder = {}

    def apply(ctx, config):
        holder["fiber"] = ctx.fiber
        return lambda: holder["fiber"].dispose()

    fiber = kernel.root.plugin(apply)
    fiber.dispose()  # used to raise ValueError from a second _fibers.remove
    assert fiber.state is FiberState.DISPOSED
    assert kernel.fibers == []


def test_dependent_disposing_its_provider_from_teardown():
    kernel = Kernel()
    provider = kernel.root.plugin(svc_provider())

    def apply(ctx, config):
        return lambda: provider.dispose()

    kernel.root.plugin(Plugin(name="d", apply=apply, inject=["x"]))
    provider.dispose()
    assert provider.state is FiberState.DISPOSED
    assert kernel.root.get("x") is None


# 2 -------------------------------------------------------------------------------
def test_dependent_never_keeps_a_service_from_a_reloaded_provider():
    kernel = Kernel()
    made: list[Svc] = []
    provider = kernel.root.plugin(svc_provider(record=made))
    held = []

    def apply(ctx, config):
        if not held:
            provider.restart()  # provider reloads while this dependent is loading
        held.append(ctx.x)

    dependent = kernel.root.plugin(Plugin(name="d", apply=apply, inject=["x"]))
    assert dependent.state is FiberState.ACTIVE
    assert dependent.ctx.x is made[-1]
    assert not dependent.ctx.x.closed


# 3 -------------------------------------------------------------------------------
def test_provide_cannot_leak_onto_a_provider_disposed_by_its_dependent():
    kernel = Kernel()
    provider = kernel.root.plugin(lambda ctx, config: None)
    kernel.root.plugin(Plugin(name="d", apply=lambda ctx, config: provider.dispose(), inject=["x"]))
    provider.ctx.provide("x", "v")
    assert provider.state is FiberState.DISPOSED
    assert kernel.root.get("x") is None
    kernel.root.plugin(lambda ctx, config: ctx.provide("x", "again"))  # no ServiceConflict


def test_effect_body_that_disposes_its_own_fiber_is_rolled_back():
    kernel = Kernel()
    log = []
    holder = {}
    fiber = kernel.root.plugin(lambda ctx, config: holder.setdefault("ctx", ctx))

    def body():
        fiber.dispose()
        return lambda: log.append("undone")

    with pytest.raises(InactiveEffectError):
        holder["ctx"].effect(body)
    assert log == ["undone"]


# 4 -------------------------------------------------------------------------------
def test_listener_of_a_plugin_disposed_mid_dispatch_does_not_fire():
    kernel = Kernel()
    calls = []
    victim = kernel.root.plugin(lambda ctx, config: ctx.on("e", lambda: calls.append("victim")))
    kernel.root.plugin(lambda ctx, config: ctx.on("e", victim.dispose, prepend=True))
    assert kernel.root.bail("e") is None
    kernel.root.emit("e")
    assert calls == []


# 5 -------------------------------------------------------------------------------
def test_mount_records_bad_plugins_and_keeps_going(tmp_path, monkeypatch):
    (tmp_path / "shapes.py").write_text(
        textwrap.dedent(
            """
            NOT_A_PLUGIN = 42
            class BadInject:
                inject = "steps"
                def __init__(self, ctx, config): pass
            def fine(ctx, config): pass
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "explodes.py").write_text("raise RuntimeError('import-time failure')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("shapes", "explodes"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    kernel = Kernel()
    report = Loader(kernel, plugins={}, runtime="0.2.0").mount(
        [
            Entry("a", "shapes:NOT_A_PLUGIN"),
            Entry("b", "shapes:BadInject"),
            Entry("c", "explodes:plugin"),
            Entry("ok", "shapes:fine"),
        ]
    )
    states = {s.id: s.state for s in report.entries}
    assert states == {"a": "skipped", "b": "skipped", "c": "skipped", "ok": "active"}
    assert "import-time failure" in report.entries[2].error


# 6 -------------------------------------------------------------------------------
def test_failed_dependent_retries_when_its_provider_restarts():
    kernel = Kernel()
    settings = {"ready": False}
    provider = kernel.root.plugin(
        Plugin(name="p", apply=lambda ctx, config: ctx.provide("x", dict(settings)))
    )

    def apply(ctx, config):
        if not ctx.x["ready"]:
            raise RuntimeError("provider not configured")

    dependent = kernel.root.plugin(Plugin(name="d", apply=apply, inject=["x"]))
    assert dependent.state is FiberState.FAILED
    settings["ready"] = True
    provider.restart()
    assert dependent.state is FiberState.ACTIVE


# 7 -------------------------------------------------------------------------------
def test_child_of_a_loading_provider_does_not_wake_outsiders_early():
    kernel = Kernel()
    log = []

    def outsider(ctx, config):
        log.append("outsider up")
        return lambda: log.append("outsider down")

    watcher = kernel.root.plugin(Plugin(name="s", apply=outsider, inject=["y"]))

    def child(ctx, config):
        ctx.provide("y", "from child")

    def parent(ctx, config):
        ctx.provide("x", 1)
        ctx.plugin(Plugin(name="c", apply=child, inject=["x"]))
        raise RuntimeError("parent fails after mounting child")

    kernel.root.plugin(parent)
    assert log == []
    assert watcher.state is FiberState.PENDING


def test_outsider_wakes_once_the_whole_providing_tree_is_active():
    kernel = Kernel()
    seen = []
    watcher = kernel.root.plugin(
        Plugin(name="s", apply=lambda ctx, config: seen.append(ctx.y), inject=["y"])
    )

    def parent(ctx, config):
        ctx.provide("x", 1)
        ctx.plugin(Plugin(name="c", apply=lambda c, k: c.provide("y", "Y"), inject=["x"]))
        assert seen == []  # parent still loading

    kernel.root.plugin(parent)
    assert seen == ["Y"]
    assert watcher.state is FiberState.ACTIVE


# 8 -------------------------------------------------------------------------------
def test_root_services_need_inject_too():
    kernel = Kernel()
    kernel.root.provide("credentials", "secret store")
    errors = []

    def nosy(ctx, config):
        try:
            ctx.credentials  # noqa: B018
        except ServiceNotInjected as exc:
            errors.append(exc)

    kernel.root.plugin(nosy)
    assert len(errors) == 1
    seen = []
    kernel.root.plugin(
        Plugin(name="ok", apply=lambda ctx, c: seen.append(ctx.credentials), inject=["credentials"])
    )
    assert seen == ["secret store"]


def test_child_does_not_inherit_parent_injected_services():
    kernel = Kernel()
    kernel.root.plugin(svc_provider("store"))
    errors = []

    def child(ctx, config):
        try:
            ctx.store  # noqa: B018
        except ServiceNotInjected as exc:
            errors.append(exc)

    def parent(ctx, config):
        ctx.plugin(child)

    kernel.root.plugin(Plugin(name="parent", apply=parent, inject=["store"]))
    assert len(errors) == 1


# 9 -------------------------------------------------------------------------------
def test_waterfall_next_is_one_shot():
    kernel = Kernel()
    executed = []

    def double_next(task, next_):
        next_()
        return next_()

    kernel.root.plugin(lambda ctx, config: ctx.on("step/pre-execute", double_next))
    with pytest.raises(KernelError, match="more than once"):
        kernel.root.waterfall("step/pre-execute", "t", default=lambda: executed.append(1))
    assert executed == [1]


# 10 ------------------------------------------------------------------------------
def test_disposing_from_inside_a_loading_plugin_is_deferred_not_lost():
    kernel = Kernel()

    def child(ctx, config):
        ctx.provide("y", 1)
        kernel.dispose()

    def parent(ctx, config):
        ctx.plugin(child)

    kernel.root.plugin(parent)
    assert kernel.fibers == []
    assert kernel.root.get("y") is None


def test_plugin_disposing_itself_during_apply_ends_disposed():
    kernel = Kernel()
    fiber = kernel.root.plugin(lambda ctx, config: ctx.fiber.dispose())
    assert fiber.state is FiberState.DISPOSED
    assert kernel.fibers == []


# minor -----------------------------------------------------------------------------
def test_effect_must_return_disposers():
    kernel = Kernel()
    fiber = kernel.root.plugin(lambda ctx, config: ctx.effect(lambda: "oops"))
    assert fiber.state is FiberState.FAILED
    assert "disposer" in str(fiber.error)


def test_profile_accepts_empty_lists(tmp_path):
    path = tmp_path / "runspool.yaml"
    path.write_text("bundles:\nrequired:\npatch:\n", encoding="utf-8")
    profile = Profile.load(path)
    assert (profile.bundles, profile.required, profile.patch) == ([], [], [])


@pytest.mark.parametrize("runtime", ["0.2.0.dev3", "0.2.0rc1", "0.2.0+local"])
def test_development_builds_count_as_their_release(runtime):
    assert check_compat("p", "1.0", ["runspool>=0.2,<0.3"], runtime) is None


def test_unknown_runtime_skips_gating():
    assert check_compat("p", "1.0", ["runspool>=0.2"], None) is None


def test_allow_matches_canonical_names():
    assert check_compat("My_Plugin", "1.0", ["runspool<0.1"], "0.2.0", ["my-plugin@1.0"]) is None


def test_null_config_in_a_patch_keeps_the_existing_config():
    warnings: list[str] = []
    out = apply_patches(
        [Entry("x", "p", config={"a": 1})], [{"id": "x", "config": None}], warnings=warnings
    )
    assert out[0].config == {"a": 1}
    assert "config: null" in warnings[0]
