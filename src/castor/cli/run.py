"""Run command: load and execute agent functions."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path


def load_agent_function(agent_spec: str) -> Callable:
    """Load an agent function from a file path, optionally with :func_name.

    Resolution order for convention mode (no :func_name):
    1. Look for 'agent' function
    2. Look for 'main' function
    3. Fail with helpful error

    Args:
        agent_spec: Path like "agent.py" or "agent.py:my_func"
    """
    if ":" in agent_spec:
        file_path, func_name = agent_spec.rsplit(":", 1)
    else:
        file_path = agent_spec
        func_name = None

    path = Path(file_path).resolve()
    if not path.exists():
        print(f"Error: file {file_path!r} not found.", file=sys.stderr)
        sys.exit(1)

    # Load module from file path
    spec = importlib.util.spec_from_file_location("_castor_agent_module", path)
    if spec is None or spec.loader is None:
        print(f"Error: cannot load {file_path!r} as Python module.", file=sys.stderr)
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if func_name:
        fn = getattr(module, func_name, None)
        if fn is None:
            print(
                f"Error: function {func_name!r} not found in {file_path}.",
                file=sys.stderr,
            )
            sys.exit(1)
        return fn

    # Convention: try 'agent', then 'main'
    for name in ("agent", "main"):
        fn = getattr(module, name, None)
        if fn is not None and callable(fn):
            return fn

    print(
        f"Error: no 'agent' or 'main' function found in {file_path}. "
        f"Use {file_path}:function_name to specify explicitly.",
        file=sys.stderr,
    )
    sys.exit(1)


def parse_budgets(budget_args: list[str] | None) -> dict[str, float] | None:
    """Parse --budget key=value arguments into a dict."""
    if not budget_args:
        return None

    budgets: dict[str, float] = {}
    for item in budget_args:
        if "=" not in item:
            print(
                f"Error: invalid budget format {item!r}, expected key=value.",
                file=sys.stderr,
            )
            sys.exit(1)
        key, value = item.split("=", 1)
        try:
            budgets[key] = float(value)
        except ValueError:
            print(f"Error: budget value {value!r} is not a number.", file=sys.stderr)
            sys.exit(1)
    return budgets


def cmd_run(args: argparse.Namespace) -> None:
    """Execute the 'castor run' command."""
    agent_fn = load_agent_function(args.agent)
    budgets = parse_budgets(args.budget)

    from castor.core import Castor

    store_uri = getattr(args, "store", None)
    kernel = Castor(
        store=store_uri,
        default_budgets=budgets,
    )

    if args.hitl == "interactive":
        from castor.hitl_policies import interactive

        cp = asyncio.run(
            kernel.run_until_complete(agent_fn, budgets=budgets, on_hitl=interactive)
        )
    else:
        cp = asyncio.run(kernel.run(agent_fn, budgets=budgets))

    print(f"\nPID:    {cp.pid}")
    print(f"Status: {cp.status}")
    if cp.result is not None:
        print(f"Result: {cp.result}")
