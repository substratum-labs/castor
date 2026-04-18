"""Blog demo: Castor Gate blocks path traversal attacks.

Shows before/after:
  - WITHOUT Castor: agent reads /etc/passwd via path traversal
  - WITH Castor: Gate blocks the traversal, agent gets a clear rejection

Run:
    uv run python examples/blog_path_traversal.py
"""

import asyncio
import os
import tempfile
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
#  Tools: read_file and write_file (the kind any research agent has)
# ═══════════════════════════════════════════════════════════════════


async def read_file(path: str) -> str:
    """Read a file from disk."""
    return Path(path).read_text()


async def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    Path(path).write_text(content)
    return f"Written {len(content)} bytes to {path}"


# ═══════════════════════════════════════════════════════════════════
#  BEFORE Castor: agent can traverse paths freely
# ═══════════════════════════════════════════════════════════════════


async def demo_without_castor():
    print("=" * 60)
    print("  BEFORE: No Castor — agent reads anything")
    print("=" * 60)
    print()

    # Simulate: agent is supposed to work in /tmp/workspace
    workspace = tempfile.mkdtemp(prefix="agent_workspace_")
    Path(workspace, "notes.txt").write_text("Agent's research notes")

    # Agent does its job...
    notes = await read_file(f"{workspace}/notes.txt")
    print(f"  ✅ read_file('{workspace}/notes.txt')")
    print(f"     → {notes!r}")
    print()

    # But then LLM hallucinates a path traversal...
    # The agent is supposed to stay in workspace, but tries /etc/hosts
    evil_path = "/etc/hosts"
    try:
        result = await read_file(evil_path)
        lines = result.strip().split("\n")[:3]
        print(f"  ⚠️  read_file('{evil_path}')")
        for line in lines:
            print(f"     │ {line}")
        print(f"     Agent read a system file! No boundary enforcement.")
    except Exception as e:
        print(f"  ❌ read_file failed: {e}")
    print()

    # Clean up
    os.unlink(f"{workspace}/notes.txt")
    os.rmdir(workspace)


# ═══════════════════════════════════════════════════════════════════
#  AFTER: Castor Gate enforces path boundaries
# ═══════════════════════════════════════════════════════════════════


async def demo_with_castor():
    from castor import Castor, castor_tool
    from castor.lib import tool

    print("=" * 60)
    print("  AFTER: Castor Gate — path traversal blocked")
    print("=" * 60)
    print()

    workspace = tempfile.mkdtemp(prefix="agent_workspace_")
    Path(workspace, "notes.txt").write_text("Agent's research notes")

    # ── Gate policy: path arguments must stay within workspace ──
    def path_validator(path: str) -> str:
        """Resolve and validate that path stays within workspace."""
        resolved = Path(path).resolve()
        workspace_resolved = Path(workspace).resolve()
        if not str(resolved).startswith(str(workspace_resolved)):
            raise ValueError(
                f"Path traversal blocked: {path!r} resolves to "
                f"{str(resolved)!r} which is outside workspace "
                f"{str(workspace_resolved)!r}"
            )
        return str(resolved)

    # Tools with path validation baked in
    @castor_tool(consumes="disk", cost_per_use=1.0)
    async def safe_read_file(path: str) -> str:
        """Read a file (path must be within workspace)."""
        validated = path_validator(path)
        return Path(validated).read_text()

    @castor_tool(consumes="disk", cost_per_use=1.0, destructive=True)
    async def safe_write_file(path: str, content: str) -> str:
        """Write a file (path must be within workspace)."""
        validated = path_validator(path)
        Path(validated).write_text(content)
        return f"Written {len(content)} bytes to {validated}"

    kernel = Castor(
        tools=[safe_read_file, safe_write_file],
        destructive=["safe_write_file"],
        budgets={"disk": 10.0},
    )

    # ── Agent tries the same thing ──

    async def research_agent():
        # Normal operation: works fine
        notes = await tool("safe_read_file", path=f"{workspace}/notes.txt")
        print(f"  ✅ safe_read_file('{workspace}/notes.txt')")
        print(f"     → {notes!r}")
        print()

        # Path traversal attempt: BLOCKED by Gate
        evil_path = "/etc/hosts"
        try:
            result = await tool("safe_read_file", path=evil_path)
            print(f"  ⚠️  safe_read_file('{evil_path}')")
            print(f"     → {result!r}")
        except Exception as e:
            print(f"  🛡️  safe_read_file('{evil_path}')")
            print(f"     → BLOCKED: {e}")
        print()

        return "Research complete"

    cp = await kernel.run(research_agent, speculative=True)

    print(f"  Status: {cp.status}")
    print(f"  Result: {cp.result}")
    print(f"  Syscalls: {len(cp.syscall_log)}")
    print(f"  Budget used: {cp.budget_used('disk'):.1f}")
    print()

    # Clean up
    os.unlink(f"{workspace}/notes.txt")
    os.rmdir(workspace)


# ═══════════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════════


async def main():
    await demo_without_castor()
    await demo_with_castor()

    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    print()
    print("  Without Castor: agent escapes workspace via ../../../")
    print("  With Castor:    Gate validates paths before execution")
    print("                  Traversal attempt → clear rejection")
    print("                  Agent continues with safe operations")
    print()
    print("  The agent code is identical. The security comes from")
    print("  the operator wrapping tools with path validation.")
    print("  Agent doesn't even know it's constrained.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
