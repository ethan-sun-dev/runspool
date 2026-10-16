"""The change guard: an application can refuse changes to its plugins once started."""

from __future__ import annotations

import pytest

from runspool.kernel import FiberState, Kernel, Plugin, PluginChangeRefused


def _provider(name, log):
    def apply(ctx, config):
        ctx.provide(name, object())
        log.append(f"load {name}")
        return lambda: log.append(f"unload {name}")

    return Plugin(name=name, apply=apply)


def _dependent(service, log):
    def apply(ctx, config):
        log.append(f"load dependent of {service}")
        ctx.plugin(lambda c, cfg: log.append("load child"), name="child")
        return lambda: log.append(f"unload dependent of {service}")

    return Plugin(name=f"needs-{service}", apply=apply, inject=[service])


def test_without_a_guard_nothing_is_checked():
    kernel = Kernel()
    log: list[str] = []
    fiber = kernel.root.plugin(_provider("a", log))
    fiber.restart()
    assert log == ["load a", "unload a", "load a"]


@pytest.mark.parametrize("change", ["restart", "dispose", "mount", "provide", "withdraw"])
def test_a_refused_change_changes_nothing(change):
    kernel = Kernel()
    log: list[str] = []
    fiber = kernel.root.plugin(_provider("a", log))
    kernel.root.plugin(_dependent("a", log))
    withdraw = fiber.ctx.provide("b", object())
    asked: list[str] = []
    kernel.guard_changes(lambda what: asked.append(what) or "frozen for the test")
    log.clear()
    fibers = kernel.fibers
    act = {
        "restart": fiber.restart,
        "dispose": fiber.dispose,
        "mount": lambda: kernel.root.plugin(_provider("c", log)),
        "provide": lambda: fiber.ctx.provide("c", object()),
        "withdraw": withdraw,
    }[change]
    with pytest.raises(PluginChangeRefused, match="frozen for the test"):
        act()
    assert len(asked) == 1
    assert log == []
    assert kernel.fibers == fibers
    assert all(f.state is FiberState.ACTIVE for f in fibers)
    assert kernel.root.get("b") is not None and kernel.root.get("c") is None


def test_an_allowed_change_is_checked_once_as_a_whole():
    """Unloading dependents and their children's reload belong to the allowed change."""
    kernel = Kernel()
    log: list[str] = []
    fiber = kernel.root.plugin(_provider("a", log))
    kernel.root.plugin(_dependent("a", log))
    asked: list[str] = []
    # Allows the first change only: anything checked again inside it would be refused.
    kernel.guard_changes(lambda what: (asked.append(what), None if len(asked) == 1 else "no")[1])
    log.clear()
    fiber.restart()
    assert asked == ["reload plugin 'a'"]
    assert log == [
        "unload dependent of a",
        "unload a",
        "load a",
        "load dependent of a",
        "load child",
    ]


def test_a_guard_that_raises_refuses():
    kernel = Kernel()
    fiber = kernel.root.plugin(_provider("a", []))

    def broken(what):
        raise RuntimeError("cannot decide")

    kernel.guard_changes(broken)
    with pytest.raises(PluginChangeRefused, match="change guard failed"):
        fiber.restart()
    assert fiber.state is FiberState.ACTIVE


def test_shutting_down_is_never_refused():
    kernel = Kernel()
    log: list[str] = []
    kernel.root.plugin(_provider("a", log))
    kernel.guard_changes(lambda what: "frozen")
    kernel.dispose()
    assert log == ["load a", "unload a"]
    assert kernel.fibers == []
    with pytest.raises(PluginChangeRefused):  # the guard applies again afterwards
        kernel.root.plugin(_provider("b", log))
