"""Castor quickstart — minimal example showing tool registration, budget, and HITL.

Run:
    cd examples && uv run python quickstart.py
"""

import asyncio

from castor import (
    AgentCheckpoint,
    AgentRunner,
    CapabilityManager,
    CastorDam,
    HITLHandler,
    SyscallProxy,
    castor_tool,
)
from castor.dam.registry import ToolRegistry

# ── 1. Register tools ──

registry = ToolRegistry()


@castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
async def web_search(query: str) -> list[str]:
    """Simulate a web search."""
    return [f"Result 1 for '{query}'", f"Result 2 for '{query}'"]


@castor_tool(
    consumes="disk",
    cost_per_use=1.0,
    destructive=True,
    requires_hitl=True,
    registry=registry,
)
def delete_files(paths: list[str]) -> int:
    """Delete files (destructive — requires human approval)."""
    print(f"  [tool] Would delete: {paths}")
    return len(paths)


# ── 2. Set up kernel ──

dam = CastorDam(registry)
cap_mgr = CapabilityManager()


# ── 3. Define an agent function ──


async def research_agent(proxy: SyscallProxy) -> str:
    """An agent that searches the web and tries to clean up temp files."""
    results = await proxy.syscall("web_search", {"query": "castor kernel"})
    print(f"  [agent] Search returned: {results}")

    # This will suspend for human approval (destructive + requires_hitl):
    deleted = await proxy.syscall("delete_files", {"paths": ["/tmp/old.log"]})
    return f"Done! Cleaned {deleted} files."


# ── 4. Run it ──


async def main() -> None:
    caps = cap_mgr.create_capabilities({"api": 10.0, "disk": 5.0})
    checkpoint = AgentCheckpoint(
        pid="quickstart-001",
        status="RUNNING",
        agent_function_name="research_agent",
        capabilities=caps,
    )

    runner = AgentRunner(dam, cap_mgr)

    # First run — will suspend at delete_files
    print("=== Run 1: agent executes until HITL suspend ===")
    checkpoint = await runner.run(research_agent, checkpoint)
    print(f"  Status: {checkpoint.status}")
    print(f"  Pending HITL: {checkpoint.pending_hitl}")

    # Human approves the destructive operation
    print("\n=== Human approves the delete ===")
    handler = HITLHandler()
    await handler.approve(checkpoint, dam, cap_mgr)
    print(f"  Status after approve: {checkpoint.status}")

    # Resume — replays past syscalls, continues from where it left off
    print("\n=== Run 2: agent resumes after approval ===")
    checkpoint = await runner.run(research_agent, checkpoint)
    print(f"  Status: {checkpoint.status}")
    print(f"  Result: {checkpoint.result}")

    # Budget check
    print("\n=== Budget usage ===")
    for name, cap in checkpoint.capabilities.items():
        used = cap.current_usage
        total = cap.max_budget
        print(f"  {name}: {used}/{total} used")


if __name__ == "__main__":
    asyncio.run(main())
