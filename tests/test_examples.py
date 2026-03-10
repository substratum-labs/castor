"""Smoke tests for example scripts — prevents demo rot.

Each test imports and runs the demo's main() function,
verifying it completes without exceptions.
"""

import sys
from pathlib import Path

import pytest

# Add examples/ to path so we can import them
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))


@pytest.fixture(autouse=True)
def _suppress_output(capsys):
    """Let demos print, but don't clutter test output."""
    yield
    capsys.readouterr()


class TestExamples:
    async def test_01_checkpoint_replay(self):
        from importlib import import_module

        mod = import_module("01_checkpoint_replay")
        await mod.main()

    async def test_02_hitl_feedback(self):
        from importlib import import_module

        mod = import_module("02_hitl_feedback")
        await mod.main()

    async def test_03_budget_guardrails(self):
        from importlib import import_module

        mod = import_module("03_budget_guardrails")
        await mod.main()

    async def test_04_preemption(self):
        from importlib import import_module

        mod = import_module("04_preemption")
        await mod.main()

    async def test_05_hero_multi_agent(self):
        from importlib import import_module

        mod = import_module("05_hero_multi_agent")
        await mod.main()

    async def test_quickstart(self):
        from importlib import import_module

        mod = import_module("quickstart")
        await mod.main()
