"""SyscallProxy: the replay gateway between agent functions and the kernel."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from castor.capability.manager import CapabilityExhaustedError, CapabilityManager
from castor.dam.validator import CastorDam

if TYPE_CHECKING:
    from castor.lodge.core import CastorLodge
    from castor.stream.agent_registry import AgentRegistry
    from castor.stream.persistence import CheckpointStore
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
        lodge: CastorLodge | None = None,
        llm_tool_names: set[str] | None = None,
        kernel_tool_names: set[str] | None = None,
        agent_registry: AgentRegistry | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self._dam = dam
        self._cap_mgr = capability_manager
        self._lodge = lodge
        self._llm_tool_names = llm_tool_names or {"llm_inference"}
        self._kernel_tool_names = kernel_tool_names or set()
        self._agent_registry = agent_registry
        self._store = checkpoint_store
        self._replay_index = 0
        # Async spawn tracking (live execution only, not persisted)
        self._async_tasks: dict[str, asyncio.Task[Any]] = {}
        self._async_checkpoints: dict[str, AgentCheckpoint] = {}

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

        # ── Lodge eviction hook: run before LLM tools (live execution only) ──
        if (
            self._lodge is not None
            and not self.is_replaying
            and tool_name in self._llm_tool_names
        ):
            await self._lodge.check_and_evict(self, self.checkpoint)

        # ── Replay: return cached response instantly ──
        # Skip kernel-internal records (e.g. sys_kernel_page_out) whose
        # side-effects are already applied to the checkpoint state.
        while self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            if record.request.get("tool_name") not in self._kernel_tool_names:
                break
            self._replay_index += 1

        if self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            if record.request != request:
                raise ReplayDivergenceError(self._replay_index, record.request, request)
            self._replay_index += 1
            return record.response

        # ── Spawn/join intercepts: kernel-internal, bypass Dam ──
        if tool_name == "spawn_agent":
            return await self._handle_spawn(request, arguments)
        if tool_name == "spawn_agent_async":
            return await self._handle_spawn_async(request, arguments)
        if tool_name == "join_agent":
            return await self._handle_join(request, arguments)

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

        # ── WAL: log intent before execution ──
        if self._store is not None:
            budget_snapshot = {
                tool_meta.consumes: self.checkpoint.capabilities[
                    tool_meta.consumes
                ].current_usage
                - tool_meta.cost_per_use
            }
            self._store.write_wal(
                pid=self.checkpoint.pid,
                syscall_index=len(self.checkpoint.syscall_log),
                tool_name=tool_name,
                arguments=validated,
                budget_snapshot=budget_snapshot,
            )

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

        # ── WAL: mark complete after execution ──
        if self._store is not None:
            self._store.complete_wal(
                pid=self.checkpoint.pid,
                syscall_index=len(self.checkpoint.syscall_log),
                result=result,
            )

        self._append_record(SyscallRecord(request=request, response=result))
        return result

    async def _handle_spawn(
        self,
        request: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Any:
        """Handle spawn_agent: delegate caps, run child, reclaim."""
        if self._agent_registry is None:
            raise RuntimeError("spawn_agent requires an AgentRegistry on SyscallProxy")

        agent_name: str = arguments["agent_name"]
        requested_caps: dict[str, float] = arguments.get("capabilities", {})

        # 1. Look up agent function
        agent_fn = self._agent_registry.get(agent_name)

        # 2. Delegate capabilities from parent to child
        child_caps = self._cap_mgr.delegate(
            self.checkpoint.capabilities, requested_caps
        )

        # 3. Deterministic child PID (count both sync and async spawns)
        spawn_count = sum(
            1
            for r in self.checkpoint.syscall_log
            if r.request.get("tool_name") in {"spawn_agent", "spawn_agent_async"}
        )
        child_pid = f"{self.checkpoint.pid}::{agent_name}-{spawn_count}"

        # 4. Create child checkpoint
        child_cp = AgentCheckpoint(
            pid=child_pid,
            parent_pid=self.checkpoint.pid,
            status="RUNNING",
            agent_function_name=agent_name,
            capabilities=child_caps,
        )

        # 5. Run child with its own proxy
        child_kernel_tools = self._lodge.kernel_tool_names if self._lodge else set()
        child_proxy = SyscallProxy(
            checkpoint=child_cp,
            dam=self._dam,
            capability_manager=self._cap_mgr,
            lodge=self._lodge,
            llm_tool_names=self._llm_tool_names,
            kernel_tool_names=child_kernel_tools,
            agent_registry=self._agent_registry,
        )

        try:
            child_result = await agent_fn(child_proxy)
            child_cp.result = child_result
            child_cp.status = "COMPLETED"
        except SuspendInterrupt:
            self._propagate_child_suspension(request, child_cp)
            raise SuspendInterrupt(self.checkpoint)
        except BaseException:
            # Child raised unexpectedly — reclaim delegated budget to prevent leak
            self._cap_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)
            raise

        # 6. Reclaim unused child budget
        self._cap_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)

        # 7. Log and return
        self._append_record(
            SyscallRecord(
                request=request,
                response=child_result,
                child_checkpoint=child_cp,
            )
        )
        return child_result

    async def _handle_spawn_async(
        self,
        request: dict[str, Any],
        arguments: dict[str, Any],
    ) -> str:
        """Handle spawn_agent_async: delegate caps, launch child task, return handle."""
        if self._agent_registry is None:
            raise RuntimeError(
                "spawn_agent_async requires an AgentRegistry on SyscallProxy"
            )

        agent_name: str = arguments["agent_name"]
        requested_caps: dict[str, float] = arguments.get("capabilities", {})

        # 1. Look up agent function
        agent_fn = self._agent_registry.get(agent_name)

        # 2. Delegate capabilities from parent to child
        child_caps = self._cap_mgr.delegate(
            self.checkpoint.capabilities, requested_caps
        )

        try:
            # 3. Deterministic child PID (count both sync and async spawns)
            spawn_count = sum(
                1
                for r in self.checkpoint.syscall_log
                if r.request.get("tool_name") in {"spawn_agent", "spawn_agent_async"}
            )
            child_pid = f"{self.checkpoint.pid}::{agent_name}-{spawn_count}"

            # 4. Create child checkpoint
            child_cp = AgentCheckpoint(
                pid=child_pid,
                parent_pid=self.checkpoint.pid,
                status="RUNNING",
                agent_function_name=agent_name,
                capabilities=child_caps,
            )

            # 5. Launch child as background task
            child_kernel_tools = self._lodge.kernel_tool_names if self._lodge else set()
            child_proxy = SyscallProxy(
                checkpoint=child_cp,
                dam=self._dam,
                capability_manager=self._cap_mgr,
                lodge=self._lodge,
                llm_tool_names=self._llm_tool_names,
                kernel_tool_names=child_kernel_tools,
                agent_registry=self._agent_registry,
            )

            async def _run_child() -> Any:
                try:
                    result = await agent_fn(child_proxy)
                    child_cp.result = result
                    child_cp.status = "COMPLETED"
                    return result
                except SuspendInterrupt:
                    # Don't re-raise — parent detects via child_cp.status at join
                    return None
                except BaseException:
                    child_cp.status = "FAILED"
                    raise

            task = asyncio.create_task(_run_child())
            self._async_tasks[child_pid] = task
            self._async_checkpoints[child_pid] = child_cp
        except BaseException:
            self._cap_mgr.reclaim(self.checkpoint.capabilities, child_caps)
            raise

        # 6. Log spawn and return handle immediately
        self._append_record(SyscallRecord(request=request, response=child_pid))
        return child_pid

    async def _handle_join(
        self,
        request: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Any:
        """Handle join_agent: await child completion, reclaim budget, return result."""
        handle: str = arguments["handle"]

        if handle not in self._async_tasks:
            raise RuntimeError(f"Unknown async agent handle: {handle!r}")

        task = self._async_tasks[handle]
        child_cp = self._async_checkpoints[handle]

        # Await child completion
        try:
            await task
        except BaseException:
            # Child raised unexpectedly — reclaim budget
            self._cap_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)
            del self._async_tasks[handle]
            del self._async_checkpoints[handle]
            raise

        # Clean up tracking
        del self._async_tasks[handle]
        del self._async_checkpoints[handle]

        # Check if child suspended for HITL
        if child_cp.status == "SUSPENDED_FOR_HITL":
            self._propagate_child_suspension(request, child_cp)
            raise SuspendInterrupt(self.checkpoint)

        # Child completed — reclaim unused budget
        self._cap_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)

        self._append_record(
            SyscallRecord(
                request=request,
                response=child_cp.result,
                child_checkpoint=child_cp,
            )
        )
        return child_cp.result

    def _propagate_child_suspension(
        self,
        request: dict[str, Any],
        child_cp: AgentCheckpoint,
    ) -> None:
        """Propagate child HITL suspension to parent."""
        self._append_record(
            SyscallRecord(
                request=request,
                response=None,
                child_checkpoint=child_cp,
            )
        )
        self.checkpoint.pending_hitl = {
            "tool_name": request["tool_name"],
            "arguments": request["arguments"],
            "child_pid": child_cp.pid,
        }
        self.checkpoint.status = "SUSPENDED_FOR_HITL"

    def _append_record(self, record: SyscallRecord) -> None:
        """Append a record to the log and advance replay index to stay in sync."""
        self.checkpoint.syscall_log.append(record)
        self._replay_index = len(self.checkpoint.syscall_log)
