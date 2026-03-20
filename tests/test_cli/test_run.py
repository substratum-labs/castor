"""Tests for castor.cli.run — agent loading and execution."""

import textwrap

import pytest


def test_load_agent_convention(tmp_path):
    """Load agent function by convention (finds 'agent' or 'main')."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def agent():
            return "hello from agent"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(str(agent_file))
    assert fn.__name__ == "agent"


def test_load_agent_explicit_func(tmp_path):
    """Load agent function by explicit name (file:func)."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def my_custom_agent():
            return "custom"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(f"{agent_file}:my_custom_agent")
    assert fn.__name__ == "my_custom_agent"


def test_load_agent_main_fallback(tmp_path):
    """Falls back to 'main' if 'agent' not found."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def main():
            return "from main"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(str(agent_file))
    assert fn.__name__ == "main"


def test_load_agent_not_found(tmp_path):
    """Raises if no agent/main function found."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text("x = 1\n")

    from castor.cli.run import load_agent_function

    with pytest.raises(SystemExit):
        load_agent_function(str(agent_file))


def test_parse_budgets():
    """Parse --budget key=value pairs."""
    from castor.cli.run import parse_budgets

    result = parse_budgets(["api_usd=0.50", "tokens=1000"])
    assert result == {"api_usd": 0.50, "tokens": 1000.0}


def test_parse_budgets_none():
    from castor.cli.run import parse_budgets

    assert parse_budgets(None) is None
