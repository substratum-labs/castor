"""Counterfactual replay engine.

Implements the core logic for replaying a session with overrides.
The ``SyscallProxy`` uses these helpers to decide, at each syscall
boundary, whether to inject an override, replay from parent journal,
or go live.
"""

from __future__ import annotations

import time
from typing import Any

from castor.models.checkpoint import SyscallPurpose, SyscallRecord
from castor.models.counterfactual import (
    CounterfactualRecord,
    OverrideNotAllowedError,
    OverrideTargetNotFoundError,
    SyscallOverride,
    digest_output,
)

# ── Allow-list ──

OVERRIDABLE_PURPOSES: frozenset[SyscallPurpose] = frozenset(
    {
        SyscallPurpose.TASK_EXECUTION,
        SyscallPurpose.MEMORY_MANAGEMENT,
    }
)

DISALLOWED_SYSCALL_NAMES: frozenset[str] = frozenset(
    {
        "spawn_agent",
        "spawn_agent_async",
        "join_agent",
        "mem_write",
        "mem_delete",
        "mem_evict",
        "mem_promote",
        "mem_protect",
    }
)


def validate_overrides(
    overrides: dict[int | str, SyscallOverride],
    syscall_log: list[SyscallRecord],
) -> dict[str, tuple[int, SyscallOverride]]:
    """Validate and resolve overrides to invocation_id-keyed dict.

    Returns ``{invocation_id: (syscall_index, override)}``.

    Raises before replay begins:
      - ``OverrideTargetNotFoundError`` if key doesn't resolve
      - ``OverrideNotAllowedError`` if syscall is in disallow-list
    """
    resolved: dict[str, tuple[int, SyscallOverride]] = {}

    for key, override in overrides.items():
        # Resolve key to (index, record)
        if isinstance(key, int):
            if key < 0 or key >= len(syscall_log):
                raise OverrideTargetNotFoundError(
                    f"syscall_index {key} out of range "
                    f"(journal has {len(syscall_log)} entries)"
                )
            record = syscall_log[key]
            idx = key
            inv_id = record.invocation_id or f"_idx_{key}"
        else:
            # key is invocation_id string
            found = False
            for i, rec in enumerate(syscall_log):
                if rec.invocation_id == key:
                    record = rec
                    idx = i
                    inv_id = key
                    found = True
                    break
            if not found:
                raise OverrideTargetNotFoundError(
                    f"invocation_id {key!r} not found in journal"
                )

        # Check allow-list
        tool_name = record.request.get("tool_name", "")
        if tool_name in DISALLOWED_SYSCALL_NAMES:
            raise OverrideNotAllowedError(
                f"cannot override syscall {tool_name!r} (in DISALLOWED_SYSCALL_NAMES)"
            )
        if record.purpose not in OVERRIDABLE_PURPOSES:
            raise OverrideNotAllowedError(
                f"cannot override syscall with purpose={record.purpose!r}"
            )

        resolved[inv_id] = (idx, override)

    return resolved


def build_counterfactual_record(
    invocation_id: str,
    syscall_index: int,
    original_output: Any,
    override: SyscallOverride,
) -> CounterfactualRecord:
    """Create a journal entry recording the override application."""
    return CounterfactualRecord(
        invocation_id=invocation_id,
        syscall_index=syscall_index,
        original_output_digest=digest_output(original_output),
        replacement_output=override.replacement_output,
        note=override.note,
        timestamp=time.time(),
    )
