"""smolagents + Castor Guard Layer Demo

Three acts demonstrating what Castor adds to smolagents:
1. Vanilla   -- no protection, all tools run freely
2. Guarded   -- budget tracking + HITL gates block destructive tools
3. Exhausted -- hard budget cap prevents runaway agent

Run: uv run python examples/framework_guards/smolagents/demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root so that ``examples`` is importable as a package.
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from smolagents import ChatMessage  # noqa: E402
from smolagents.models import Model  # noqa: E402

from castor.capability.manager import CapabilityExhaustedError  # noqa: E402
from examples.framework_guards.smolagents.guard import (  # noqa: E402
    CastorGuardedAgent,
    ToolRejectedError,
)
from examples.framework_guards.smolagents.tools import (  # noqa: E402
    read_file,
    send_message,
    web_search,
    write_file,
)


# ---------------------------------------------------------------------------
# Fake LLM -- never actually called; we drive tools directly via
# execute_tool_call to keep the demo deterministic and dependency-free.
# ---------------------------------------------------------------------------
class FakeModel(Model):
    """Stub model that satisfies smolagents' ToolCallingAgent constructor."""

    def __init__(self):
        super().__init__(model_id="fake")

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ) -> ChatMessage:
        return ChatMessage(role="assistant", content="(FakeModel: not used in demo)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TOOLS = [web_search, read_file, write_file, send_message]

DIVIDER = "=" * 60


def banner(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def call(agent, tool_name: str, arguments: dict) -> str | None:
    """Call a tool via the agent and print the result or error."""
    try:
        result = agent.execute_tool_call(tool_name, arguments)
        print(f"  {tool_name}(...) -> {result}")
        return result
    except ToolRejectedError as exc:
        print(f"  {tool_name}(...) -> REJECTED: {exc}")
        return None
    except CapabilityExhaustedError as exc:
        print(f"  {tool_name}(...) -> BLOCKED: {exc}")
        return None


# ---------------------------------------------------------------------------
# Act 1: Vanilla smolagents -- no guardrails
# ---------------------------------------------------------------------------
def act1() -> None:
    banner("ACT 1: Vanilla smolagents -- zero protection")

    from smolagents import ToolCallingAgent

    agent = ToolCallingAgent(tools=TOOLS, model=FakeModel())

    call(agent, "web_search", {"query": "latest LLM benchmarks"})
    call(agent, "read_file", {"filename": "secrets.env"})
    call(agent, "write_file", {"filename": "config.yaml", "content": "admin: true"})
    call(
        agent,
        "send_message",
        {"recipient": "#general", "body": "I changed the config!"},
    )

    print("\n  No budget tracking. No approval gate. All four tools ran.")


# ---------------------------------------------------------------------------
# Act 2: CastorGuardedAgent -- HITL gates on destructive tools
# ---------------------------------------------------------------------------
def act2() -> None:
    banner("ACT 2: CastorGuardedAgent -- HITL gates on destructive tools")

    # Programmatic HITL policy: approve write_file, reject send_message
    def hitl_policy(tool_name: str, _arguments) -> bool:
        return tool_name != "send_message"

    agent = CastorGuardedAgent(
        tools=TOOLS,
        model=FakeModel(),
        budgets={"network": 10.0, "disk": 5.0},
        tool_policies={
            "web_search": {"resource": "network", "cost": 1.0, "destructive": False},
            "read_file": {"resource": "disk", "cost": 0.5, "destructive": False},
            "write_file": {"resource": "disk", "cost": 2.0, "destructive": True},
            "send_message": {"resource": "network", "cost": 1.5, "destructive": True},
        },
        hitl_policy=hitl_policy,
    )

    print("  Policy: approve write_file, REJECT send_message\n")

    call(agent, "web_search", {"query": "latest LLM benchmarks"})
    call(agent, "read_file", {"filename": "notes.txt"})
    call(agent, "write_file", {"filename": "report.md", "content": "# Summary\n..."})
    call(agent, "send_message", {"recipient": "#general", "body": "Publishing report"})

    print("\n  Budget summary:")
    for resource, info in agent.budget_summary().items():
        remaining = info["remaining"]
        print(
            f"    {resource}: {info['used']:.1f}"
            f" / {info['max']:.1f}"
            f"  (remaining {remaining:.1f})"
        )

    print(f"\n  Audit log ({len(agent.audit_log)} entries):")
    for entry in agent.audit_log:
        tool = entry["tool"]
        cost = entry["cost"]
        res = entry["resource"]
        print(f"    {tool:15s}  cost={cost:.1f}  resource={res}")


# ---------------------------------------------------------------------------
# Act 3: Budget exhaustion -- hard cap stops runaway agent
# ---------------------------------------------------------------------------
def act3() -> None:
    banner("ACT 3: Budget exhaustion -- hard cap stops runaway")

    agent = CastorGuardedAgent(
        tools=TOOLS,
        model=FakeModel(),
        budgets={"network": 2.5},
        tool_policies={
            "web_search": {"resource": "network", "cost": 1.0, "destructive": False},
        },
        hitl_policy=lambda _name, _args: True,  # approve everything
    )

    print("  Budget: network = 2.5 | web_search costs 1.0 each\n")

    call(agent, "web_search", {"query": "query 1"})
    call(agent, "web_search", {"query": "query 2"})
    call(agent, "web_search", {"query": "query 3"})  # should fail -- only 0.5 left

    print("\n  Budget summary:")
    for resource, info in agent.budget_summary().items():
        remaining = info["remaining"]
        print(
            f"    {resource}: {info['used']:.1f}"
            f" / {info['max']:.1f}"
            f"  (remaining {remaining:.1f})"
        )


# ---------------------------------------------------------------------------
# Epilogue
# ---------------------------------------------------------------------------
def epilogue() -> None:
    banner("THE DIFF: what changed between Act 1 and Act 2?")
    print("""
  Vanilla (Act 1):
    agent = ToolCallingAgent(tools=TOOLS, model=model)

  Guarded (Act 2):
    agent = CastorGuardedAgent(
        tools=TOOLS, model=model,
        budgets={"network": 10.0, "disk": 5.0},
        tool_policies={...},
        hitl_policy=my_policy,
    )

  Same tools. Same model. One constructor swap adds:
    - Per-resource budget enforcement
    - HITL approval gates for destructive tools
    - Full audit log of every tool call
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    act1()
    act2()
    act3()
    epilogue()
