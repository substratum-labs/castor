"""Run command: load and execute agent functions."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path


def load_function(spec: str) -> Callable:
    """Load a function from a 'module:func' or 'file.py:func' spec.

    Supports:
      - 'module.path:func_name' — importlib-based
      - 'file.py:func_name' — file-based
      - 'file.py' — convention lookup (agent, then main)
    """
    if ":" in spec:
        module_path, func_name = spec.rsplit(":", 1)
    else:
        module_path = spec
        func_name = None

    # Try as file path first
    path = Path(module_path)
    if path.suffix == ".py" and path.exists():
        module = _load_module_from_file(path)
    else:
        # Try as Python module path
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            # Maybe it's a file without .py suffix?
            path_py = Path(f"{module_path}.py")
            if path_py.exists():
                module = _load_module_from_file(path_py)
            else:
                print(f"Error: cannot import {module_path!r}.", file=sys.stderr)
                sys.exit(1)

    if func_name:
        fn = getattr(module, func_name, None)
        if fn is None:
            print(f"Error: {func_name!r} not found in {module_path}.", file=sys.stderr)
            sys.exit(1)
        return fn

    # Convention: try 'agent', then 'main'
    for name in ("agent", "main"):
        fn = getattr(module, name, None)
        if fn is not None and callable(fn):
            return fn

    print(
        f"Error: no 'agent' or 'main' function found in {module_path}. "
        f"Use {module_path}:func_name to specify explicitly.",
        file=sys.stderr,
    )
    sys.exit(1)


load_agent_function = load_function  # backward compat


def _load_module_from_file(path: Path):
    """Load a Python module from a file path."""
    resolved = path.resolve()
    spec = importlib.util.spec_from_file_location("_castor_agent_module", resolved)
    if spec is None or spec.loader is None:
        print(f"Error: cannot load {path!r} as Python module.", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    agent_fn = load_function(args.agent)
    budgets = parse_budgets(args.budget)

    # Load tool functions
    tools = None
    if args.tools:
        tools = [load_function(t) for t in args.tools]

    # Load LLM function
    llm = None
    if args.llm:
        llm = load_function(args.llm)

    from castor.core import Castor

    store_uri = getattr(args, "store", None)
    kernel = Castor(
        tools=tools,
        destructive=args.destructive if args.destructive else None,
        llm=llm,
        store=store_uri,
        default_budgets=budgets,
    )

    speculative = getattr(args, "speculative", False)

    if args.hitl == "interactive" and not speculative:
        from castor.hitl_policies import interactive

        cp = asyncio.run(
            kernel.run_until_complete(agent_fn, budgets=budgets, on_hitl=interactive)
        )
    else:
        cp = asyncio.run(kernel.run(agent_fn, budgets=budgets, speculative=speculative))

    # Output
    print(f"\nPID:    {cp.pid}")
    print(f"Status: {cp.status}")
    if cp.result is not None:
        result_str = str(cp.result)[:200]
        print(f"Result: {result_str}")

    # Execution summary for speculative mode
    if speculative and cp.status == "COMPLETED":
        summary = kernel.scan(cp)
        print("\nExecution Summary:")
        print(f"  Total steps:    {summary.total_steps}")
        print(f"  Auto-verified:  {summary.auto_verified}")
        print(f"  Flagged:        {summary.flagged_count}")
        if summary.flagged:
            for f in summary.flagged:
                print(f"    Step {f.index}: {f.tool_name} — {f.reason}")
