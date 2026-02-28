"""SyscallProxy: the replay gateway between agent functions and the kernel."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from castor.capability.manager import CapabilityExhaustedError, CapabilityManager
from castor.dam.validator import CastorDam
from castor.models.capability import SyscallResponse
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)


class ReplayDivergenceError(Exception):
    """Raised when a replay request doesn't match the recorded syscall."""

    def __init__(self, index: int, expected: dict[str, Any], actual: dict[str, Any]):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Replay divergence at index {index}: expected {expected}, got {actual}"
        )


class SyscallProxy:
    """Injected into every agent function. All side effects go through this.

    Decides: replay from cache, execute (Fast Path), or suspend (Slow Path).

    WARNING: Every non-deterministic operation — network calls, LLM inference,
    file I/O, random number generation — MUST be routed through
    ``await proxy.syscall(tool_name, args)``.  Direct calls bypass the
    ``syscall_log`` and will break replay determinism: on resume the call
    re-executes live, produces a different result, and the agent issues a
    divergent syscall sequence (``ReplayDivergenceError``).

    For LLM inference specifically, use ``castor.llm.LLMSyscall`` to wrap
    your provider client as a registered Castor tool.
    """

    def __init__(
        self,
        checkpoint: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
    ) -> None:
        self.checkpoint = checkpoint
        self._dam = dam
        self._cap_mgr = capability_manager
        self._replay_index = 0

    @property
    def is_replaying(self) -> bool:
        """True if the proxy is still serving cached responses."""
        return self._replay_index < len(self.checkpoint.syscall_log)

    async def syscall(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Main syscall entry point.

        Flow:
        1. Replay path: serve cached response if available
        2. Validate via Dam
        3. Slow path: suspend if HITL required
        4. Fast path: deduct capability, execute, log result
        """
        request = {"tool_name": tool_name, "arguments": arguments}

        # ── Replay: return cached response instantly ──
        if self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            if record.request != request:
                raise ReplayDivergenceError(self._replay_index, record.request, request)
            self._replay_index += 1
            return record.response

        # ── New syscall: validate via Dam ──
        try:
            validated = self._dam.validate(tool_name, arguments)
        except ValidationError as e:
            # Return validation error as a response (not an exception)
            # so the LLM can self-correct
            response = self._dam.format_validation_error(tool_name, e)
            self._append_record(
                SyscallRecord(request=request, response=response.model_dump())
            )
            return response.model_dump()

        tool_meta = self._dam.get_tool_meta(tool_name)

        # ── Slow Path: suspend for HITL ──
        if tool_meta.requires_hitl or tool_meta.destructive:
            self.checkpoint.pending_hitl = request
            self.checkpoint.status = "SUSPENDED_FOR_HITL"
            raise SuspendInterrupt(self.checkpoint)

        # ── Fast Path: deduct capability, execute, log ──
        try:
            self._cap_mgr.deduct(
                self.checkpoint.capabilities,
                tool_meta.consumes,
                tool_meta.cost_per_use,
            )
        except CapabilityExhaustedError as e:
            response = SyscallResponse(
                status="INSUFFICIENT_CAPABILITY",
                feedback_message=str(e),
            )
            self._append_record(
                SyscallRecord(request=request, response=response.model_dump())
            )
            return response.model_dump()

        try:
            result = await self._dam.execute(tool_name, validated)
        except BaseException:
            # Refund the budget — execution was interrupted (CancelledError from
            # preemption) or failed (tool exception).  Without this, the record
            # is never logged, so replay will re-attempt the syscall and deduct
            # again, causing a permanent budget leak.
            self._cap_mgr.refund(
                self.checkpoint.capabilities,
                tool_meta.consumes,
                tool_meta.cost_per_use,
            )
            raise

        self._append_record(SyscallRecord(request=request, response=result))
        return result

    def _append_record(self, record: SyscallRecord) -> None:
        """Append a record to the log and advance replay index to stay in sync."""
        self.checkpoint.syscall_log.append(record)
        self._replay_index = len(self.checkpoint.syscall_log)
