"""The daemon's maintenance hook: ``daemon/tick`` once per round, never fatal."""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from runspool.app import load_context
from runspool.daemon import Daemon, DaemonTick


class _Coordinator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ticks = 0
        self.pool = SimpleNamespace(shutdown=lambda wait=True: None)

    def reclaim_stale(self, timeout):
        pass

    def tick(self):
        self.ticks += 1
        if self.fail:
            raise RuntimeError("scheduling broke")


def _config():
    return SimpleNamespace(
        worker_pool=SimpleNamespace(heartbeat_timeout_seconds=60),
        scheduler=SimpleNamespace(poll_interval_seconds=0),
    )


def _run(daemon: Daemon, *, until) -> None:
    daemon.recover = lambda: None  # no store here
    thread = threading.Thread(target=daemon.run, kwargs={"poll_interval_seconds": 0.001})
    thread.start()
    for _ in range(2000):
        if until():
            break
        threading.Event().wait(0.001)
    daemon.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_each_round_ends_with_a_tick():
    seen: list[DaemonTick] = []
    coordinator = _Coordinator()
    daemon = Daemon(coordinator, _config(), on_tick=seen.append)
    _run(daemon, until=lambda: len(seen) >= 3)
    assert [t.round for t in seen[:3]] == [1, 2, 3]
    assert seen[0].now <= seen[2].now
    assert coordinator.ticks >= 3


def test_a_failing_hook_or_round_does_not_stop_the_daemon(caplog):
    calls = []

    def hook(tick):
        calls.append(tick.round)
        raise ValueError("hook broke")

    with caplog.at_level(logging.ERROR):
        daemon = Daemon(_Coordinator(fail=True), _config(), on_tick=hook)
        _run(daemon, until=lambda: len(calls) >= 3)
    assert calls[:3] == [1, 2, 3]  # the hook still runs after a failed round
    assert "maintenance hook failed" in caplog.text
    assert "daemon tick failed" in caplog.text


def test_the_runtime_daemon_emits_daemon_tick_to_plugins(tmp_path, caplog):
    profile = tmp_path / "runspool.yaml"
    profile.write_text(f"workspace_root: {tmp_path / 'ws'}\n", encoding="utf-8")
    ctx = load_context(profile)
    seen = []

    def broken(tick):
        raise RuntimeError("listener broke")

    ctx.kernel.root.plugin(lambda c, config: c.on("daemon/tick", broken))
    ctx.kernel.root.plugin(lambda c, config: c.on("daemon/tick", seen.append))
    daemon = ctx.service("runtime").build_daemon()
    tick = DaemonTick(round=1, now=0.0)
    with caplog.at_level(logging.ERROR):
        daemon.on_tick(tick)
    assert seen == [tick]  # one listener failing does not starve the others
    assert "listener broke" in caplog.text
