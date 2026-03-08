"""Castor CLI: Checkpoint inspection and HITL decision recording.

Usage::

    castor list                           # List all checkpoints
    castor show <pid>                     # Show checkpoint details
    castor reject <pid> --feedback "..."  # Reject pending HITL
    castor modify <pid> --feedback "..."  # Modify pending HITL with feedback

Note: ``approve`` is not supported via CLI because it requires executing
the blocked tool (Dam + CapabilityManager).  Use the host application's
resume loop to approve and execute.
"""

from __future__ import annotations

import argparse
import json
import sys

from castor.scheduler.hitl import HITLHandler
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore

_STATUS_MARKERS = {
    "SUSPENDED_FOR_HITL": "HITL",
    "COMPLETED": "DONE",
    "RUNNING": "RUN ",
    "PREEMPTED": "PREM",
    "FAILED": "FAIL",
}


def cmd_list(store: CheckpointStore, _args: argparse.Namespace) -> None:
    """List all checkpoint PIDs with status."""
    pids = store.list_pids()
    if not pids:
        print("No checkpoints found.")
        return

    for pid in pids:
        cp = store.load(pid)
        marker = _STATUS_MARKERS.get(cp.status, "??? ")
        line = f"  [{marker}] {pid}"
        if cp.pending_hitl:
            tool = cp.pending_hitl.get("tool_name", "?")
            line += f"  (pending: {tool})"
        print(line)


def cmd_show(store: CheckpointStore, args: argparse.Namespace) -> None:
    """Show detailed checkpoint information."""
    try:
        cp = store.load(args.pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {args.pid!r} not found.", file=sys.stderr)
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


def cmd_reject(store: CheckpointStore, args: argparse.Namespace) -> None:
    """Reject pending HITL request."""
    try:
        cp = store.load(args.pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {args.pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(
            f"Error: checkpoint {args.pid!r} has no pending HITL.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {args.pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)
    handler.reject(cp, args.feedback)
    store.save(cp)
    print(f"Rejected: {args.pid}")


def cmd_modify(store: CheckpointStore, args: argparse.Namespace) -> None:
    """Modify pending HITL request with feedback."""
    try:
        cp = store.load(args.pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {args.pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(
            f"Error: checkpoint {args.pid!r} has no pending HITL.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {args.pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)
    handler.modify(cp, args.feedback)
    store.save(cp)
    print(f"Modified: {args.pid}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="castor",
        description="Castor: inspect and manage agent checkpoints",
    )
    parser.add_argument(
        "--db",
        default="castor.db",
        help="SQLite database path (default: castor.db)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all checkpoints")

    show_p = sub.add_parser("show", help="Show checkpoint details")
    show_p.add_argument("pid", help="Checkpoint PID")

    reject_p = sub.add_parser("reject", help="Reject pending HITL")
    reject_p.add_argument("pid", help="Checkpoint PID")
    reject_p.add_argument("--feedback", required=True, help="Rejection reason")

    modify_p = sub.add_parser("modify", help="Modify pending HITL with feedback")
    modify_p.add_argument("pid", help="Checkpoint PID")
    modify_p.add_argument("--feedback", required=True, help="Modification feedback")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    store = CheckpointStore(f"sqlite:///{args.db}")

    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "reject": cmd_reject,
        "modify": cmd_modify,
    }
    commands[args.command](store, args)


if __name__ == "__main__":
    main()
