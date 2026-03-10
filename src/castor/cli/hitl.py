"""HITL commands: reject, modify."""

from __future__ import annotations

import sys

from castor.scheduler.hitl import HITLHandler
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore


def cmd_reject(store: CheckpointStore, pid: str, reason: str) -> None:
    """Reject a pending HITL request."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(f"Error: checkpoint {pid!r} has no pending HITL.", file=sys.stderr)
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler.reject(cp, reason)
    store.save(cp)
    print(f"Rejected: {pid}")


def cmd_modify(store: CheckpointStore, pid: str, feedback: str) -> None:
    """Modify a pending HITL request with feedback."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(f"Error: checkpoint {pid!r} has no pending HITL.", file=sys.stderr)
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler.modify(cp, feedback)
    store.save(cp)
    print(f"Modified: {pid}")
