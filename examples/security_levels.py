#!/usr/bin/env python3
"""Castor Security Levels Demo

Same agent, same task, three security modes.
Shows how Castor protects your agent at different trust levels.

    Level 1: HITL — every dangerous operation pauses for approval
    Level 2: Speculative — full speed, review after (needs sandbox)
    Level 3: Time-Travel — rewind and fix mistakes

Usage:
    cd castor/
    uv run python examples/security_levels.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

# ── Simulated file system (so we don't touch real files) ──

_filesystem: dict[str, str] = {
    "/var/log/app.log.old": "old app logs...",
    "/var/log/nginx.log.7": "old nginx logs...",
    "/var/log/data.db.bak": "IMPORTANT: production database backup!",
    "/var/log/error.log.3": "old error logs...",
    "/var/log/app.log": "current app logs (active)",
    "/var/log/nginx.log": "current nginx logs (active)",
    "/var/log/cleanup_policy.txt": (
        "Policy: delete .old, .bak, and numbered log files older than 7 days.\n"
        "Exception: never delete files containing 'IMPORTANT' in content."
    ),
}


def _reset_fs():
    """Reset filesystem to initial state."""
    global _filesystem
    _filesystem = {
        "/var/log/app.log.old": "old app logs...",
        "/var/log/nginx.log.7": "old nginx logs...",
        "/var/log/data.db.bak": "IMPORTANT: production database backup!",
        "/var/log/error.log.3": "old error logs...",
        "/var/log/app.log": "current app logs (active)",
        "/var/log/nginx.log": "current nginx logs (active)",
        "/var/log/cleanup_policy.txt": (
            "Policy: delete .old, .bak, and numbered log files older than 7 days.\n"
            "Exception: never delete files containing 'IMPORTANT' in content."
        ),
    }


# ── Tools (plain functions — no Castor knowledge) ──


async def list_files(directory: str) -> str:
    """List files in a directory."""
    files = [f for f in _filesystem if f.startswith(directory)]
    return "\n".join(sorted(files)) if files else "(empty)"


async def read_file(path: str) -> str:
    """Read a file's contents."""
    content = _filesystem.get(path)
    if content is None:
        return f"Error: {path} not found"
    return content


async def delete_file(path: str) -> str:
    """Delete a file. This is irreversible."""
    if path not in _filesystem:
        return f"Error: {path} not found"
    del _filesystem[path]
    return f"Deleted {path}"


async def write_report(path: str, content: str) -> str:
    """Write a report file."""
    _filesystem[path] = content
    return f"Wrote report to {path} ({len(content)} chars)"


# ── Agent function (doesn't know about Castor) ──


async def cleanup_agent(proxy):
    """Agent that cleans up old log files. Has a bug: ignores the IMPORTANT exception."""
    # Step 1: List files
    files = await proxy.syscall("list_files", {"directory": "/var/log"})

    # Step 2: Read policy
    policy = await proxy.syscall("read_file", {"path": "/var/log/cleanup_policy.txt"})

    # Step 3-6: Delete old files (the agent has a bug — it doesn't check for IMPORTANT)
    deleted = []
    for f in files.strip().split("\n"):
        if f.endswith((".old", ".bak")) or any(f.endswith(f".{i}") for i in range(10)):
            result = await proxy.syscall("delete_file", {"path": f})
            deleted.append(f"{f}: {result}")

    # Step 7: Write report
    report = f"Cleanup complete.\nPolicy: {policy[:50]}...\nActions:\n" + "\n".join(deleted)
    await proxy.syscall("write_report", {"path": "/var/log/cleanup_report.txt", "content": report})

    return f"Cleaned up {len(deleted)} files"


# ── Demo runner ──


def _print_header(title: str, style: str = "="):
    width = 65
    print(f"\n{style * width}")
    print(f"  {title}")
    print(f"{style * width}")


def _print_fs_state(label: str):
    """Show current filesystem state."""
    print(f"\n  📂 Filesystem after {label}:")
    for path in sorted(_filesystem):
        marker = "⚠️ " if "IMPORTANT" in _filesystem[path] else "   "
        print(f"    {marker}{path}")
    important_exists = any("IMPORTANT" in v for v in _filesystem.values())
    if not important_exists:
        print(f"    ❌ data.db.bak is GONE — production backup deleted!")
    print()


async def level1_hitl():
    """Level 1: HITL mode — every destructive op pauses for approval."""
    from castor import Castor
    from castor.hitl_policies import auto_approve

    _reset_fs()
    _print_header("LEVEL 1: HITL — Every dangerous op needs approval")

    kernel = Castor(
        tools=[list_files, read_file, delete_file, write_report],
        destructive=["delete_file", "write_report"],
    )

    print("\n  Mode: HITL (default)")
    print("  Destructive tools pause for human approval.")
    print("  Safe tools execute immediately.\n")

    # Simulate: auto-approve first 3 deletes, reject the 4th (data.db.bak)
    hitl_count = 0
    decisions = []

    async def smart_hitl(cp):
        nonlocal hitl_count
        hitl_count += 1
        tool = cp.pending_hitl.get("tool_name", "?")
        args = cp.pending_hitl.get("arguments", {})

        if tool == "delete_file" and "data.db.bak" in args.get("path", ""):
            print(f"  ⏸  HITL #{hitl_count}: {tool}({args})")
            print(f"      → 🛑 REJECTED — this is a production backup!")
            decisions.append(("reject", f"#{hitl_count} {tool}: REJECTED"))
            return ("reject", "This file contains IMPORTANT data — do not delete")
        else:
            print(f"  ⏸  HITL #{hitl_count}: {tool}({args})")
            print(f"      → ✅ approved")
            decisions.append(("approve", f"#{hitl_count} {tool}: approved"))
            return ("approve", None)

    t0 = time.perf_counter()
    cp = await kernel.run_until_complete(
        cleanup_agent, budgets={"api": 50.0}, on_hitl=smart_hitl
    )
    elapsed = time.perf_counter() - t0

    _print_fs_state("Level 1")

    print(f"  📊 Results:")
    print(f"     Status: {cp.status}")
    print(f"     Steps: {len(cp.syscall_log)}")
    print(f"     HITL interruptions: {hitl_count}")
    print(f"     Time: {elapsed:.2f}s (includes HITL pauses)")
    for d in decisions:
        print(f"       {d[1]}")

    important_safe = any("IMPORTANT" in v for v in _filesystem.values())
    print(f"\n  🛡️  Production backup safe? {'YES ✅' if important_safe else 'NO ❌'}")


async def level2_speculative():
    """Level 2: Speculative mode — full speed, review after."""
    from castor import Castor

    _reset_fs()
    _print_header("LEVEL 2: SPECULATIVE — Full speed, review after")

    kernel = Castor(
        tools=[list_files, read_file, delete_file, write_report],
        destructive=["delete_file", "write_report"],
    )

    print("\n  Mode: Speculative")
    print("  All tools execute without pausing.")
    print("  Destructive ops flagged with needs_review for post-hoc audit.\n")

    t0 = time.perf_counter()
    cp = await kernel.run(cleanup_agent, budgets={"api": 50.0}, speculative=True)
    elapsed = time.perf_counter() - t0

    summary = kernel.scan(cp)

    _print_fs_state("Level 2")

    print(f"  📊 Execution Summary:")
    print(f"     Status: {cp.status}")
    print(f"     Steps: {summary.total_steps}")
    print(f"     Auto-verified: {summary.auto_verified} ✅")
    print(f"     Flagged: {summary.flagged_count} ⚠️")
    print(f"     Time: {elapsed:.2f}s (zero interruptions)")

    for f in summary.flagged:
        args_str = str(f.arguments)[:50]
        print(f"       Step {f.index}: {f.tool_name}({args_str}) — {f.reason}")

    important_safe = any("IMPORTANT" in v for v in _filesystem.values())
    print(f"\n  🛡️  Production backup safe? {'YES ✅' if important_safe else 'NO ❌'}")

    if not important_safe:
        print(f"\n  ⚠️  Problem: data.db.bak was deleted!")
        print(f"     In production with sandbox: rollback would undo this.")
        print(f"     Without sandbox: the damage is done — but we detected it.")
        print(f"     → Proceed to Level 3: Time-Travel to see the fix.")

    return cp, kernel


async def level3_timetravel(cp_from_level2, kernel):
    """Level 3: Time-Travel — rewind and fix the mistake."""
    _print_header("LEVEL 3: TIME-TRAVEL — Rewind and fix")

    print("\n  Starting from Level 2's checkpoint.")
    print(f"  Level 2 had {len(cp_from_level2.syscall_log)} steps.\n")

    # Find the bad step (delete of data.db.bak)
    bad_step = None
    for i, record in enumerate(cp_from_level2.syscall_log):
        if (
            record.request.get("tool_name") == "delete_file"
            and "data.db.bak" in str(record.request.get("arguments", {}))
        ):
            bad_step = i
            break

    if bad_step is None:
        print("  (Agent didn't delete data.db.bak — no mistake to fix)")
        return

    print(f"  🔍 Found the mistake: Step {bad_step} deleted data.db.bak")
    print(f"  ⏪ Rewinding to Step {bad_step} (keeping steps 0-{bad_step - 1})")

    # Fork — rewind to before the bad step
    forked = cp_from_level2.fork(at_step=bad_step)
    print(f"  🔀 Forked: {forked.pid}")
    print(f"     Cached steps: {len(forked.syscall_log)} (free replay)")

    # Reset filesystem and replay the cached steps' effects
    _reset_fs()
    for record in forked.syscall_log:
        tool = record.request.get("tool_name")
        args = record.request.get("arguments", {})
        if tool == "delete_file":
            path = args.get("path", "")
            if path in _filesystem:
                del _filesystem[path]

    print(f"\n  ▶️  Re-running same agent from Step {bad_step}...")
    print(f"     This time data.db.bak is protected (simulated sandbox).\n")

    # Same agent, but we protect data.db.bak by making delete_file skip it
    original_delete = delete_file

    async def safe_delete_file(path: str) -> str:
        """Delete with protection — reject IMPORTANT files."""
        content = _filesystem.get(path, "")
        if "IMPORTANT" in content:
            return f"BLOCKED: {path} contains IMPORTANT data — skipped"
        return await original_delete(path)

    # Re-create kernel with the safe delete
    from castor import Castor

    safe_kernel = Castor(
        tools=[list_files, read_file, ("delete_file", safe_delete_file), write_report],
        destructive=["delete_file", "write_report"],
    )

    t0 = time.perf_counter()
    cp2 = await safe_kernel.run(
        cleanup_agent, checkpoint=forked, budgets={"api": 50.0}, speculative=True
    )
    elapsed = time.perf_counter() - t0

    _print_fs_state("Level 3 (Time-Travel)")

    # Compare
    a_steps = len(cp_from_level2.syscall_log)
    b_steps = len(cp2.syscall_log)
    cached = len(forked.syscall_log)

    print(f"  📊 Timeline Comparison:")
    print(f"     Timeline A (original):  {a_steps} steps, data.db.bak deleted ❌")
    print(f"     Timeline B (fixed):     {b_steps} steps, data.db.bak preserved ✅")
    print(f"     Steps cached (free):    {cached}")
    print(f"     Time for fix:           {elapsed:.2f}s")

    important_safe = any("IMPORTANT" in v for v in _filesystem.values())
    print(f"\n  🛡️  Production backup safe? {'YES ✅' if important_safe else 'NO ❌'}")


async def main():
    print()
    print("  ╔═══════════════════════════════════════════════════════╗")
    print("  ║  CASTOR — Secure Execution Layer for LLM Agents      ║")
    print("  ║  Same task. Three security levels.                    ║")
    print("  ╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Task: Agent cleans up old log files in /var/log")
    print("  Bug:  Agent ignores the 'IMPORTANT' exception in policy")
    print("  Risk: Production database backup (data.db.bak) may be deleted")

    # Level 1: HITL
    await level1_hitl()

    # Level 2: Speculative
    cp, kernel = await level2_speculative()

    # Level 3: Time-Travel
    await level3_timetravel(cp, kernel)

    # Summary
    _print_header("SUMMARY: Three Levels of Protection")
    print("""
  Level 1: HITL (no sandbox needed)
    ✅ Human approves every dangerous operation
    ✅ Caught the mistake before it happened
    ⚠️  Slow — 4+ interruptions for 4 destructive ops

  Level 2: Speculative (needs sandbox for full safety)
    ✅ Zero interruptions — full speed execution
    ✅ Post-hoc summary identifies all dangerous steps
    ⚠️  Without sandbox, destructive ops are real

  Level 3: Time-Travel (checkpoint/replay)
    ✅ Rewind to before the mistake
    ✅ Cached steps replay for free
    ✅ Fix the agent and re-run from the fork point
    ✅ Works with both Level 1 and Level 2

  Choose your level based on trust and infrastructure:
    Low trust, no sandbox  → Level 1 (HITL)
    High trust, sandbox    → Level 2 (Speculative)
    Any trust, any setup   → Level 3 (Time-Travel) for recovery
    """)

    print("  pip install castor-kernel")
    print("  https://github.com/substratum-labs/castor")
    print()


if __name__ == "__main__":
    asyncio.run(main())
