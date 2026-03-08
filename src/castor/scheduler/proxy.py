"""SyscallProxy: the replay gateway between agent functions and the kernel."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from castor.capability.manager import CapabilityExhaustedError, CapabilityManager
from castor.gate.validator import SyscallGate
from castor.observability import get_logger, get_meter

if TYPE_CHECKING:
    from castor.mmu.core import MMU
    from castor.scheduler.agent_registry import AgentRegistry
    from castor.scheduler.persistence import CheckpointStore
from castor.models.capability import SyscallResponse
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.models.result import SyscallResult

_logger = get_logger("castor.scheduler")
_meter = get_meter("castor.scheduler")
_syscall_counter = _meter.create_counter("castor_syscalls_total")
_syscall_duration = _meter.create_histogram("castor_syscall_duration_seconds")
_hitl_counter = _meter.create_counter("castor_hitl_total")
_spawn_counter = _meter.create_counter("castor_spawns_total")


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
        gate: SyscallGate,
        capability_manager: CapabilityManager,
        lodge: MMU | None = None,
        llm_tool_names: set[str] | None = None,
        kernel_tool_names: set[str] | None = None,
        agent_registry: AgentRegistry | None = None,
        checkpoint_store: CheckpointStore | None = None,
        structured_results: bool = False,
    ) -> None:
        self.checkpoint = checkpoint
        self._gate = gate
        self._cap_mgr = capability_manager
        self._lodge = lodge
        self._llm_tool_names = llm_tool_names or {"llm_inference"}
        self._kernel_tool_names = kernel_tool_names or set()
        self._agent_registry = agent_registry
        self._store = checkpoint_store
        self._structured_results = structured_results
        self._replay_index = 0
        # Cached spawn count — computed once from syscall_log at init, then
        # incremented per spawn.  Avoids O(N) log scan on every spawn call.
        self._spawn_count = sum(
            1
            for r in checkpoint.syscall_log
            if r.request.get("tool_name") in {"spawn_agent", "spawn_agent_async"}
        )
        # Async spawn tracking (live execution only, not persisted)
        self._async_tasks: dict[str, asyncio.Task[Any]] = {}
        self._async_checkpoints: dict[str, AgentCheckpoint] = {}

    @property
    def is_replaying(self) -> bool:
        """True if the proxy is still serving cached responses."""
        return self._replay_index < len(self.checkpoint.syscall_log)

    async def syscall(
        self, tool_name: str, arguments: dict[str, Any] | None = None, /, **kwargs: Any
    ) -> Any:
        """Main syscall entry point.

        Flow:
        1. Replay path: serve cached response if available
        2. Validate via Gate
        3. Slow path: suspend if HITL required
        4. Fast path: deduct capability, execute, log result

        Args:
            tool_name: Name of the registered tool to invoke.
            arguments: Tool arguments as a dict (positional-only).
            **kwargs: Tool arguments as keyword arguments (alternative to dict).

        Raises:
            TypeError: If both ``arguments`` dict and ``**kwargs`` are provided.
        """
        if arguments is not None and kwargs:
            raise TypeError(
                "Cannot pass both positional arguments dict and keyword arguments"
            )
        if arguments is None:
            arguments = kwargs
        request = {"tool_name": tool_name, "arguments": arguments}
        start = time.perf_counter()

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
            _logger.debug(
                "replay_hit",
                extra={
                    "pid": self.checkpoint.pid,
                    "tool": tool_name,
                    "index": self._replay_index - 1,
                },
            )
            return self._wrap_if_needed(tool_name, record.response)

        # ── Spawn/join intercepts: kernel-internal, bypass Dam ──
        if tool_name == "spawn_agent":
            return await self._handle_spawn(request, arguments)
        if tool_name == "spawn_agent_async":
            return await self._handle_spawn_async(request, arguments)
        if tool_name == "join_agent":
            return await self._handle_join(request, arguments)

        # ── New syscall: validate via Gate ──
        try:
            validated = self._gate.validate(tool_name, arguments)
        except ValidationError as e:
            # Return validation error as a response (not an exception)
            # so the LLM can self-correct
            response = self._gate.format_validation_error(tool_name, e)
            self._append_record(
                SyscallRecord(request=request, response=response.model_dump())
            )
            return response.model_dump()

        tool_meta = self._gate.get_tool_meta(tool_name)

        # ── Slow Path: suspend for HITL ──
        if tool_meta.requires_hitl or tool_meta.destructive:
            _hitl_counter.add(1, {"action": "suspend"})
            _logger.info(
                "hitl_suspend",
                extra={
                    "pid": self.checkpoint.pid,
                    "tool": tool_name,
                },
            )
            self.checkpoint.pending_hitl = request
            self.checkpoint.status = "SUSPENDED_FOR_HITL"
            raise SuspendInterrupt(self.checkpoint)

        # ── Fast Path: deduct capability, execute, log ──
        # Zero-cost tools skip budget checks entirely — they may use a
        # default resource type ("_default") that has no matching capability.
        if tool_meta.cost_per_use > 0:
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
                resp_dict = response.model_dump()
                self._append_record(SyscallRecord(request=request, response=resp_dict))
                return self._wrap_if_needed(tool_name, resp_dict)

        # ── WAL: log intent before execution ──
        # Snapshot captures usage BEFORE deduction so recover() restores correctly.
        wal_syscall_index = len(self.checkpoint.syscall_log)
        if self._store is not None:
            budget_snapshot = {
                tool_meta.consumes: self.checkpoint.capabilities[
                    tool_meta.consumes
                ].current_usage
                - tool_meta.cost_per_use
            }
            self._store.write_wal(
                pid=self.checkpoint.pid,
                syscall_index=wal_syscall_index,
                tool_name=tool_name,
                arguments=validated,
                budget_snapshot=budget_snapshot,
            )

        try:
            if tool_meta.timeout_seconds is not None and not tool_meta.is_async:
                # Sync tool with timeout: run in thread executor
                from concurrent.futures import ThreadPoolExecutor

                loop = asyncio.get_running_loop()
                pool = ThreadPoolExecutor(max_workers=1)
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(pool, lambda: tool_meta.func(**validated)),
                        timeout=tool_meta.timeout_seconds,
                    )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
            elif tool_meta.timeout_seconds is not None:
                # Async tool with timeout
                result = await asyncio.wait_for(
                    self._gate.execute(tool_name, validated),
                    timeout=tool_meta.timeout_seconds,
                )
            else:
                result = await self._gate.execute(tool_name, validated)
        except BaseException:
            # Abandon WAL entry — tool did not complete successfully
            if self._store is not None:
                self._store.abandon_wal(self.checkpoint.pid, wal_syscall_index)
            # Refund the budget — execution was interrupted (CancelledError from
            # preemption) or failed (tool exception).  Without this, the record
            # is never logged, so replay will re-attempt the syscall and deduct
            # again, causing a permanent budget leak.
            if tool_meta.cost_per_use > 0:
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
                syscall_index=wal_syscall_index,
                result=result,
            )

        elapsed = time.perf_counter() - start
        _syscall_counter.add(1, {"tool": tool_name, "status": "success"})
        _syscall_duration.record(elapsed, {"tool": tool_name})
        _logger.info(
            "syscall_complete",
            extra={
                "pid": self.checkpoint.pid,
                "tool": tool_name,
                "latency_ms": elapsed * 1000,
            },
        )

        self._append_record(SyscallRecord(request=request, response=result))
        return self._wrap_if_needed(tool_name, result)

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

        # 3. Deterministic child PID (cached counter — O(1) per spawn)
        child_pid = f"{self.checkpoint.pid}::{agent_name}-{self._spawn_count}"
        self._spawn_count += 1

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
            gate=self._gate,
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
        _spawn_counter.add(1, {"type": "sync"})
        _logger.info(
            "spawn",
            extra={
                "pid": self.checkpoint.pid,
                "child_pid": child_cp.pid,
                "type": "sync",
            },
        )
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
            # 3. Deterministic child PID (cached counter — O(1) per spawn)
            child_pid = f"{self.checkpoint.pid}::{agent_name}-{self._spawn_count}"
            self._spawn_count += 1

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
                gate=self._gate,
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

            # Persist child checkpoint for observability
            if self._store is not None:
                self._store.save(child_cp)

            task = asyncio.create_task(_run_child())
            self._async_tasks[child_pid] = task
            self._async_checkpoints[child_pid] = child_cp
        except BaseException:
            self._cap_mgr.reclaim(self.checkpoint.capabilities, child_caps)
            raise

        # 6. Log spawn and return handle immediately
        _spawn_counter.add(1, {"type": "async"})
        _logger.info(
            "spawn",
            extra={
                "pid": self.checkpoint.pid,
                "child_pid": child_pid,
                "type": "async",
            },
        )
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

    # ── Preemption context helpers ──

    @property
    def preemption_context(self) -> dict[str, Any] | None:
        """Return preemption metadata if this is a resume after preemption.

        Returns ``None`` if the checkpoint was not preempted.  Otherwise
        returns a dict with ``reason``, ``payload``, and ``partial_work``.
        The agent can inspect this on resume to adapt its behaviour.
        """
        if self.checkpoint.preemption_reason is None:
            return None
        return {
            "reason": self.checkpoint.preemption_reason,
            "payload": self.checkpoint.preemption_payload,
            "partial_work": self.checkpoint.partial_work,
        }

    def clear_preemption_context(self) -> None:
        """Clear preemption fields after the agent has consumed them."""
        self.checkpoint.preemption_reason = None
        self.checkpoint.preemption_payload = None
        self.checkpoint.partial_work = None

    def charge_partial(self, resource_type: str, cost: float) -> None:
        """Re-deduct actual cost after an automatic full refund.

        When a streaming tool is cancelled mid-execution, the proxy's
        ``BaseException`` handler refunds the full ``cost_per_use``.  The
        streaming wrapper then calls this method to charge the actual
        amount consumed (e.g. ``tokens * cost_per_token``).
        """
        try:
            self._cap_mgr.deduct(self.checkpoint.capabilities, resource_type, cost)
        except CapabilityExhaustedError:
            # Budget is already exhausted — charge whatever remains.
            cap = self.checkpoint.capabilities.get(resource_type)
            if cap is not None:
                cap.current_usage = cap.max_budget

    def _append_record(self, record: SyscallRecord) -> None:
        """Append a record to the log and advance replay index to stay in sync."""
        self.checkpoint.syscall_log.append(record)
        self._replay_index = len(self.checkpoint.syscall_log)

    def _wrap_if_needed(self, tool_name: str, response: Any) -> Any:
        """Wrap response in SyscallResult for destructive/HITL tools."""
        if not self._structured_results:
            return response
        if not self._gate.registry.has_tool(tool_name):
            return response
        meta = self._gate.get_tool_meta(tool_name)
        if not (meta.requires_hitl or meta.destructive):
            return response
        if isinstance(response, dict):
            status = response.get("status")
            if status == "HITL_REJECTED":
                return SyscallResult(
                    status="HITL_REJECTED",
                    feedback=response.get("human_feedback"),
                )
            if status == "HITL_MODIFIED":
                return SyscallResult(
                    status="HITL_MODIFIED",
                    feedback=response.get("human_feedback"),
                )
            if status == "INSUFFICIENT_CAPABILITY":
                return SyscallResult(
                    status="INSUFFICIENT_CAPABILITY",
                    feedback=response.get("feedback_message"),
                    resource=meta.consumes,
                )
        return SyscallResult(value=response)

    def budget(self, resource: str) -> float:
        """Return remaining budget for a resource type.

        Returns 0.0 if the resource is not tracked.
        """
        cap = self.checkpoint.capabilities.get(resource)
        if cap is None:
            return 0.0
        return cap.max_budget - cap.current_usage

    async def call(self, func: Any, /, **kwargs: Any) -> Any:
        """Call a tool by function reference.

        Usage: await proxy.call(search, query="hello")
        The function must be decorated with @castor_tool.
        """
        meta = getattr(func, "_castor_metadata", None)
        if meta is None:
            raise TypeError(
                f"{func!r} is not a @castor_tool — "
                f"only decorated functions can be used with proxy.call()"
            )
        return await self.syscall(meta.tool_name, **kwargs)

    async def spawn(
        self, agent_name: str, *, capabilities: dict[str, float] | None = None
    ) -> str:
        """Spawn a child agent asynchronously and return a join handle.

        Sugar for ``proxy.syscall("spawn_agent_async", ...)``.
        """
        return await self.syscall(
            "spawn_agent_async",
            agent_name=agent_name,
            capabilities=capabilities or {},
        )

    async def join(self, handle: str) -> Any:
        """Wait for a spawned child agent to complete and return its result.

        Sugar for ``proxy.syscall("join_agent", ...)``.
        """
        return await self.syscall("join_agent", handle=handle)

    async def spawn_sync(
        self, agent_name: str, *, capabilities: dict[str, float] | None = None
    ) -> Any:
        """Spawn a child agent synchronously and wait for its result.

        Sugar for ``proxy.syscall("spawn_agent", ...)``.
        """
        return await self.syscall(
            "spawn_agent",
            agent_name=agent_name,
            capabilities=capabilities or {},
        )

    def __getattr__(self, name: str) -> Any:
        """Enable proxy.tool_name(...) style calls.

        Returns an async callable that delegates to syscall().
        Only triggers for names not found via normal attribute lookup.
        """
        # Check if it's a registered tool
        if self._gate.registry.has_tool(name):

            async def _tool_call(**kwargs: Any) -> Any:
                return await self.syscall(name, **kwargs)

            return _tool_call

        raise AttributeError(
            f"'{type(self).__name__}' has no attribute '{name}' "
            f"and '{name}' is not a registered tool"
        )
