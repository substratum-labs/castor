"""Process management commands: ps, inspect."""

from __future__ import annotations

import json
import sys

from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore

_STATUS_MARKERS = {
    "SUSPENDED_FOR_HITL": "HITL",
    "COMPLETED": "DONE",
    "RUNNING": "RUN ",
    "PREEMPTED": "PREM",
    "FAILED": "FAIL",
}


def cmd_ps(store: CheckpointStore) -> None:
    """List all agent processes with status."""
    pids = store.list_pids()
    if not pids:
        print("No agents found.")
        return

    for pid in pids:
        cp = store.load(pid)
        marker = _STATUS_MARKERS.get(cp.status, "??? ")
        line = f"  [{marker}] {pid}"
        if cp.pending_hitl:
            tool = cp.pending_hitl.get("tool_name", "?")
            line += f"  (pending: {tool})"
        print(line)


def cmd_inspect(store: CheckpointStore, pid: str) -> None:
    """Show detailed checkpoint information."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"PID:    {cp.pid}")
    print(f"Status: {cp.status}")
    print(f"Agent:  {cp.agent_function_name}")
    if cp.parent_pid:
        print(f"Parent: {cp.parent_pid}")

    print("\nCapabilities:")
    for name, cap in cp.capabilities.items():
        remaining = cap.max_budget - cap.current_usage
        print(f"  {name}: {remaining:.1f} / {cap.max_budget:.1f} remaining")

    print(f"\nSyscall log: {len(cp.syscall_log)} entries")

    if cp.pending_hitl:
        print("\n--- Pending HITL ---")
        print(f"  Tool:      {cp.pending_hitl.get('tool_name')}")
        print(f"  Arguments: {json.dumps(cp.pending_hitl.get('arguments'), indent=4)}")
        if "child_pid" in cp.pending_hitl:
            print(f"  Child PID: {cp.pending_hitl['child_pid']}")

    if cp.result is not None:
        print(f"\nResult: {cp.result}")
