"""Event dispatch modes and listener lifetime."""

from __future__ import annotations

import logging

import pytest

from runspool.kernel import Kernel


def listen(kernel: Kernel, event: str, callback, *, prepend: bool = False):
    return kernel.root.plugin(lambda ctx, config: ctx.on(event, callback, prepend=prepend))


def test_emit_isolates_failing_listeners(caplog):
    kernel = Kernel()
    got = []

    def bad(value):
        raise RuntimeError("listener broke")

    listen(kernel, "task/transitioned", bad)
    listen(kernel, "task/transitioned", got.append)
    with caplog.at_level(logging.ERROR):
        kernel.root.emit("task/transitioned", 7)
    assert got == [7]
    assert "listener broke" in caplog.text


def test_bail_returns_first_decisive_result():
    kernel = Kernel()
    calls = []
    listen(kernel, "check", lambda v: calls.append("a"))  # None: keep going
    listen(kernel, "check", lambda v: False)  # False: keep going
    listen(kernel, "check", lambda v: f"veto {v}")
    listen(kernel, "check", lambda v: calls.append("never"))
    assert kernel.root.bail("check", 1) == "veto 1"
    assert calls == ["a"]
    assert Kernel().root.bail("check", 1) is None


def test_bail_propagates_exceptions():
    kernel = Kernel()

    def bad(value):
        raise ValueError("no")

    listen(kernel, "check", bad)
    with pytest.raises(ValueError):
        kernel.root.bail("check", 1)


def test_waterfall_runs_as_middleware_and_can_veto():
    kernel = Kernel()
    trace = []

    def outer(task, next_):
        trace.append("outer in")
        result = next_()
        trace.append("outer out")
        return result

    def deny_drafts(task, next_):
        if task == "draft":
            return "deny"
        return next_()

    listen(kernel, "step/pre-execute", outer)
    listen(kernel, "step/pre-execute", deny_drafts)
    assert kernel.root.waterfall("step/pre-execute", "draft", default=lambda: "allow") == "deny"
    assert kernel.root.waterfall("step/pre-execute", "fetch", default=lambda: "allow") == "allow"
    assert trace == ["outer in", "outer out", "outer in", "outer out"]


def test_waterfall_without_listeners_returns_default():
    assert Kernel().root.waterfall("x", default=lambda: 42) == 42


def test_prepend_puts_listener_first():
    kernel = Kernel()
    order = []
    listen(kernel, "e", lambda: order.append("normal"))
    listen(kernel, "e", lambda: order.append("first"), prepend=True)
    kernel.root.emit("e")
    assert order == ["first", "normal"]


def test_listener_is_removed_when_its_plugin_unloads():
    kernel = Kernel()
    got = []
    fiber = listen(kernel, "e", got.append)
    kernel.root.emit("e", 1)
    fiber.dispose()
    kernel.root.emit("e", 2)
    assert got == [1]
