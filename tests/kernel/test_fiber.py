"""Kernel lifecycle: loading, dependency injection, effects, failure rollback, disposal."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from runspool.kernel import (
    FiberState,
    InactiveEffectError,
    Kernel,
    KernelError,
    Plugin,
    PluginShapeError,
    ServiceConflict,
    ServiceNotInjected,
)


def provider(name: str, value: object, log: list[str] | None = None, *, inject=()):
    def apply(ctx, config):
        ctx.provide(name, value)
        if log is not None:
            log.append(f"load {name}")
            return lambda: log.append(f"unload {name}")
        return None

    return Plugin(name=f"provide-{name}", apply=apply, inject=inject)


def consumer(service: str, seen: list, log: list[str] | None = None):
    def apply(ctx, config):
        seen.append(getattr(ctx, service))
        if log is not None:
            log.append(f"load consumer({service})")
            return lambda: log.append(f"unload consumer({service})")
        return None

    return Plugin(name=f"use-{service}", apply=apply, inject=[service])


# -- plugin shapes and config ----------------------------------------------------


def test_function_object_and_class_plugins_load():
    kernel = Kernel()
    calls = []

    def fn(ctx, config):
        calls.append(("fn", config))

    class Obj:
        name = "obj"

        def apply(self, ctx, config):
            calls.append(("obj", config))

    class Cls:
        def __init__(self, ctx, config):
            calls.append(("cls", config))

    for plugin in (fn, Obj(), Cls):
        assert kernel.root.plugin(plugin, {"x": 1}).state is FiberState.ACTIVE
    assert calls == [("fn", {"x": 1}), ("obj", {"x": 1}), ("cls", {"x": 1})]


def test_non_plugin_is_rejected():
    with pytest.raises(PluginShapeError):
        Kernel().root.plugin(42)


def test_config_is_validated_and_defaulted():
    class Config(BaseModel):
        retries: int = 3
        name: str

    got = []
    plugin = Plugin(name="p", apply=lambda ctx, config: got.append(config), Config=Config)
    fiber = Kernel().root.plugin(plugin, {"name": "a"})
    assert fiber.state is FiberState.ACTIVE
    assert got[0].retries == 3 and got[0].name == "a"


def test_invalid_config_fails_only_that_plugin():
    class Config(BaseModel):
        retries: int

    kernel = Kernel()
    bad = kernel.root.plugin(Plugin(name="bad", apply=lambda c, k: None, Config=Config), {})
    good = kernel.root.plugin(lambda ctx, config: None)
    assert bad.state is FiberState.FAILED
    assert "retries" in str(bad.error)
    assert good.state is FiberState.ACTIVE
    with pytest.raises(Exception, match="retries"):
        bad.wait()


# -- dependency injection --------------------------------------------------------


def test_consumer_waits_for_provider_regardless_of_order():
    kernel = Kernel()
    seen: list = []
    user = kernel.root.plugin(consumer("store", seen))
    assert user.state is FiberState.PENDING
    assert user.missing_services() == ["store"]
    kernel.root.plugin(provider("store", "S"))
    assert user.state is FiberState.ACTIVE
    assert seen == ["S"]


def test_undeclared_service_is_not_reachable():
    kernel = Kernel()
    kernel.root.plugin(provider("store", "S"))
    errors = []

    def sneaky(ctx, config):
        try:
            ctx.store  # noqa: B018 - the read itself is the test
        except ServiceNotInjected as exc:
            errors.append(exc)
        assert not hasattr(ctx, "store")
        assert ctx.get("store") == "S"  # the optional path still works

    kernel.root.plugin(sneaky)
    assert len(errors) == 1


def test_provider_is_not_visible_until_its_apply_finishes():
    kernel = Kernel()
    seen: list = []
    states = []

    def slow_provider(ctx, config):
        ctx.provide("store", "S")
        # Mounted while the provider is still loading: must not activate yet.
        states.append(kernel.root.plugin(consumer("store", seen)).state)

    kernel.root.plugin(slow_provider)
    assert states == [FiberState.PENDING]
    assert seen == ["S"]  # activated right after the provider became ACTIVE


def test_failed_provider_rolls_back_and_never_wakes_dependents():
    kernel = Kernel()
    seen: list = []
    kernel.root.plugin(consumer("store", seen))

    def broken(ctx, config):
        ctx.provide("store", "S")
        raise RuntimeError("boom")

    fiber = kernel.root.plugin(broken)
    assert fiber.state is FiberState.FAILED
    assert seen == []
    assert kernel.root.get("store") is None  # the provide was rolled back


def test_dependents_unload_before_provider_teardown():
    kernel = Kernel()
    log: list[str] = []
    seen: list = []
    store = kernel.root.plugin(provider("store", "S", log))
    user = kernel.root.plugin(consumer("store", seen, log))
    store.dispose()
    assert log == ["load store", "load consumer(store)", "unload consumer(store)", "unload store"]
    assert user.state is FiberState.PENDING


def test_dependency_chain_cascades():
    kernel = Kernel()
    log: list[str] = []
    a = kernel.root.plugin(provider("a", 1, log))
    kernel.root.plugin(provider("b", 2, log, inject=["a"]))
    seen: list = []
    kernel.root.plugin(consumer("b", seen, log))
    a.dispose()
    assert log == [
        "load a",
        "load b",
        "load consumer(b)",
        "unload consumer(b)",
        "unload b",
        "unload a",
    ]


def test_chain_activates_bottom_up_whatever_the_mount_order():
    kernel = Kernel()
    log: list[str] = []
    seen: list = []
    user = kernel.root.plugin(consumer("b", seen, log))
    middle = kernel.root.plugin(provider("b", 2, log, inject=["a"]))
    assert (user.state, middle.state) == (FiberState.PENDING, FiberState.PENDING)
    kernel.root.plugin(provider("a", 1, log))
    assert log == ["load a", "load b", "load consumer(b)"]
    assert seen == [2]


def test_provider_swap_reloads_dependents_against_the_new_provider():
    kernel = Kernel()
    seen: list = []
    first = kernel.root.plugin(provider("store", "sqlite"))
    user = kernel.root.plugin(consumer("store", seen))
    first.dispose()
    kernel.root.plugin(provider("store", "memory"))
    assert seen == ["sqlite", "memory"]
    assert user.state is FiberState.ACTIVE


def test_failed_plugin_retries_when_its_providers_change():
    kernel = Kernel()
    attempts = []

    def picky(ctx, config):
        attempts.append(ctx.store)
        if ctx.store == "bad":
            raise RuntimeError("bad store")

    bad = kernel.root.plugin(provider("store", "bad"))
    fiber = kernel.root.plugin(Plugin(name="picky", apply=picky, inject=["store"]))
    assert fiber.state is FiberState.FAILED
    bad.dispose()
    kernel.root.plugin(provider("store", "good"))
    assert fiber.state is FiberState.ACTIVE
    assert attempts == ["bad", "good"]


def test_duplicate_service_name_is_rejected():
    kernel = Kernel()
    kernel.root.plugin(provider("store", 1))
    fiber = kernel.root.plugin(provider("store", 2))
    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, ServiceConflict)


def test_reserved_service_names_are_rejected():
    kernel = Kernel()
    fiber = kernel.root.plugin(lambda ctx, config: ctx.provide("plugin", 1))
    assert isinstance(fiber.error, KernelError)


# -- effects and disposal --------------------------------------------------------


def test_effects_are_undone_in_reverse_order():
    kernel = Kernel()
    log: list[str] = []

    def apply(ctx, config):
        for name in ("a", "b", "c"):

            def register(name=name):
                log.append(f"+{name}")
                return lambda: log.append(f"-{name}")

            ctx.effect(register)

    fiber = kernel.root.plugin(apply)
    fiber.dispose()
    assert log == ["+a", "+b", "+c", "-c", "-b", "-a"]


def test_effect_can_be_disposed_early_and_only_once():
    kernel = Kernel()
    log: list[str] = []
    holder = {}

    def apply(ctx, config):
        holder["undo"] = ctx.effect(lambda: lambda: log.append("undone"))

    fiber = kernel.root.plugin(apply)
    holder["undo"]()
    holder["undo"]()
    fiber.dispose()
    assert log == ["undone"]


def test_failing_disposer_is_logged_and_teardown_continues(caplog):
    kernel = Kernel()
    log: list[str] = []

    def boom():
        raise RuntimeError("disposer exploded")

    def apply(ctx, config):
        ctx.effect(lambda: lambda: log.append("first"))
        ctx.effect(lambda: boom)

    fiber = kernel.root.plugin(apply)
    with caplog.at_level(logging.ERROR):
        fiber.dispose()
    assert log == ["first"]
    assert "disposer exploded" in caplog.text


def test_effects_after_disposal_are_refused():
    kernel = Kernel()
    holder = {}
    fiber = kernel.root.plugin(lambda ctx, config: holder.setdefault("ctx", ctx))
    fiber.dispose()
    with pytest.raises(InactiveEffectError):
        holder["ctx"].effect(lambda: None)
    with pytest.raises(InactiveEffectError):
        holder["ctx"].plugin(lambda ctx, config: None)


def test_child_plugins_go_with_their_parent():
    kernel = Kernel()
    log: list[str] = []

    def child(ctx, config):
        log.append("child up")
        return lambda: log.append("child down")

    def parent(ctx, config):
        ctx.plugin(child)
        return lambda: log.append("parent down")

    fiber = kernel.root.plugin(parent)
    fiber.dispose()
    assert log == ["child up", "child down", "parent down"]
    assert kernel.fibers == []


def test_child_can_use_service_its_parent_provides_during_apply():
    kernel = Kernel()
    seen: list = []

    def parent(ctx, config):
        ctx.provide("store", "S")
        ctx.plugin(consumer("store", seen))

    kernel.root.plugin(parent)
    assert seen == ["S"]


def test_class_plugin_dispose_method_runs_on_unload():
    kernel = Kernel()
    log: list[str] = []

    class Service:
        name = "svc"

        def __init__(self, ctx, config):
            ctx.provide("svc", self)

        def dispose(self):
            log.append("disposed")

    fiber = kernel.root.plugin(Service)
    assert kernel.root.get("svc") is not None
    fiber.dispose()
    assert log == ["disposed"]
    assert kernel.root.get("svc") is None


def test_restart_reruns_apply():
    kernel = Kernel()
    runs = []
    fiber = kernel.root.plugin(lambda ctx, config: runs.append(1))
    fiber.restart()
    assert runs == [1, 1]
    assert fiber.state is FiberState.ACTIVE


def test_kernel_dispose_unloads_everything_newest_first():
    kernel = Kernel()
    log: list[str] = []
    kernel.root.plugin(provider("a", 1, log))
    kernel.root.plugin(provider("b", 2, log))
    kernel.dispose()
    assert log == ["load a", "load b", "unload b", "unload a"]
    assert kernel.fibers == []


def test_context_bound_service_gives_each_plugin_its_own_view():
    kernel = Kernel()
    registry: list[str] = []

    class Steps:
        def for_context(self, ctx):
            outer = self

            class View:
                def register(self, name):
                    return ctx.effect(
                        lambda: (registry.append(name), lambda: registry.remove(name))[1]
                    )

                owner = ctx.fiber.name
                shared = outer

            return View()

    kernel.root.plugin(
        Plugin(name="steps", apply=lambda ctx, config: ctx.provide("steps", Steps()))
    )
    views = []

    def contributor(ctx, config):
        views.append(ctx.steps)
        ctx.steps.register("greet")

    fiber = kernel.root.plugin(Plugin(name="greeter", apply=contributor, inject=["steps"]))
    assert registry == ["greet"]
    assert views[0].owner == "greeter"
    assert kernel.root.get("steps").owner == "root"
    fiber.dispose()
    assert registry == []  # the registration was an effect of the contributor
