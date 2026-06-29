import pytest

from runspool.engine.registry import StepRegistry
from runspool.engine.step import Step, StepContext, StepResult


class _Dummy(Step):
    name = "dummy"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult()


def test_register_get_has():
    reg = StepRegistry()
    reg.register(_Dummy())
    assert reg.has("dummy")
    assert reg.get("dummy").name == "dummy"
    assert "dummy" in reg.names()


def test_duplicate_registration_rejected():
    reg = StepRegistry()
    reg.register(_Dummy())
    with pytest.raises(ValueError):
        reg.register(_Dummy())


def test_missing_step_raises_keyerror():
    reg = StepRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")
