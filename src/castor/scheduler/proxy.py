"""SyscallProxy: the replay gateway between agent functions and the kernel."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from pydantic import ValidationError

from castor.budget.manager import BudgetExhaustedError
from castor.gate.registry import prepare_execution_arguments
from castor.kernel.decisions import (
    Allow,
    Deny,
    ReplayDivergenceError,  # noqa: F401 — re-exported via scheduler.__init__
    ReplayHit,
    Suspend,
    decide_syscall,
)
from castor.kernel.journal import InMemoryJournal
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    SuspendInterrupt,
    SyscallPurpose,
    SyscallRecord,
    compute_invocation_id,
)
from castor.models.result import SyscallResult
from castor.observability import get_logger, get_meter
from castor.protocols import (
    AgentRegistryProtocol,
    BudgetProtocol,
    CheckpointStoreProtocol,
    GateProtocol,
    MMUProtocol,
)

_logger = get_logger("castor.scheduler")
_meter = get_meter("castor.scheduler")
_syscall_counter = _meter.create_counter("castor_syscalls_total")
_syscall_duration = _meter.create_histogram("castor_syscall_duration_seconds")
_hitl_counter = _meter.create_counter("castor_hitl_total")
_spawn_counter = _meter.create_counter("castor_spawns_total")

# Memory syscall names — used to tag SyscallRecord.purpose and trigger
# post-syscall effects (eviction application, cold storage persistence).
# AISA §2.2 memory syscall names.
_MEMORY_SYSCALL_NAMES = frozenset(
    {
        "mem_write",
        "mem_read",
        "mem_search",
        "mem_delete",
        "mem_evict",
        "mem_promote",
        "mem_protect",
    }
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
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        lodge: MMUProtocol | None = None,
        llm_tool_names: set[str] | None = None,
        kernel_tool_names: set[str] | None = None,
        agent_registry: AgentRegistryProtocol | None = None,
        checkpoint_store: CheckpointStoreProtocol | None = None,
        structured_results: bool = False,
        speculative: bool = False,
        scheduler: Any | None = None,
        cf_overrides: dict[str, Any] | None = None,
        cf_mode: str | None = None,
        cf_parent_log: list[Any] | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self._gate = gate
        self._budget_mgr = capability_manager
        self._lodge = lodge
        self._scheduler = scheduler
        # Counterfactual replay state.
        # If no explicit overrides are passed but the checkpoint has a
        # counterfactual_log, reconstruct overrides from it so replaying
        # a saved CF session reproduces the same overrides.
        if not cf_overrides and checkpoint.counterfactual_log:
            from castor.models.counterfactual import SyscallOverride

            cf_overrides = {}
            for rec in checkpoint.counterfactual_log:
                cf_overrides[rec.invocation_id] = SyscallOverride(
                    replacement_output=rec.replacement_output,
                    note=rec.note,
                )
        self._cf_overrides = cf_overrides or {}
        self._cf_mode = cf_mode
        self._cf_parent_log = cf_parent_log or []
        self._past_divergence = False
        self._llm_tool_names = llm_tool_names or {"llm_inference"}
        self._kernel_tool_names = kernel_tool_names or set()
        self._agent_registry = agent_registry
        self._store = checkpoint_store
        self._structured_results = structured_results
        self._speculative = speculative
        self._journal = InMemoryJournal(checkpoint.syscall_log)
        self._replay_index = 0
        # Cached spawn count — computed once from journal at init, then
        # incremented per spawn.  Avoids O(N) log scan on every spawn call.
        self._spawn_count = sum(
            1
            for _, r in self._journal.scan_from(0)
            if r.request.get("tool_name") in {"spawn_agent", "spawn_agent_async"}
        )
        # Async spawn tracking (live execution only, not persisted)
        self._async_tasks: dict[str, asyncio.Task[Any]] = {}
        self._async_checkpoints: dict[str, AgentCheckpoint] = {}

        # Priority-ordered spawn queue for sequential dispatch.
        # Entries: (-priority, creation_order, child_pid, agent_fn, child_cp)
        # Negative priority so heapq (min-heap) pops highest priority first.
        self._spawn_queue: list[tuple[int, int, str, Any, AgentCheckpoint]] = []
        self._spawn_queue_seq = 0

    @property
    def is_replaying(self) -> bool:
        """True if the proxy is still serving cached responses."""
        return self._replay_index < len(self._journal)

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

        # ── Preemption check ──
        # Scheduler inspects checkpoint state and decides whether to
        # preempt. Runs BEFORE dispatch — if the agent should stop,
        # it never reaches the tool. Deterministic: same checkpoint →
        # same decision → replay reproduces exact preempt point.
        if self.checkpoint.status in ("PREEMPTED", "BUDGET_EXHAUSTED"):
            # Already preempted — all subsequent syscalls blocked.
            from castor.models.preemption import PreemptedError, PreemptionReason

            reason = PreemptionReason.BUDGET_EXHAUSTED
            meta: dict[str, Any] = {}
            if self.checkpoint.preemption_log:
                last = self.checkpoint.preemption_log[-1]
                reason = last.reason
                meta = last.metadata
            raise PreemptedError(reason, metadata=meta)

        if self._scheduler is not None and not self.is_replaying:
            preempt = self._scheduler.should_preempt(self.checkpoint)
            if preempt is not None:
                reason, metadata = preempt
                from castor.models.checkpoint import PreemptionRecord
                from castor.models.preemption import PreemptedError

                record = PreemptionRecord(
                    syscall_index_after=len(self.checkpoint.syscall_log),
                    reason=reason,
                    timestamp=time.time(),
                    metadata=metadata,
                )
                self.checkpoint.preemption_log.append(record)
                self.checkpoint.status = "PREEMPTED"
                raise PreemptedError(reason=reason, metadata=metadata)

        # ``operation_id`` is a kernel-reserved argument.  Never accept an
        # agent-supplied value: it would turn an idempotency key into prompt
        # controlled data.  The kernel injects its own value at execution.
        arguments = dict(arguments)
        arguments.pop("operation_id", None)
        request = {"tool_name": tool_name, "arguments": arguments}
        start = time.perf_counter()

        # ── Counterfactual override (early check) ──
        # If this syscall has a CF override, inject it immediately
        # without going through replay/allow paths. Works whether or
        # not the journal has a cached entry for this step.
        if self._cf_overrides:
            from castor.models.checkpoint import compute_invocation_id
            from castor.scheduler.counterfactual import build_counterfactual_record

            pre_inv_id = compute_invocation_id(
                self.checkpoint.pid,
                len(self._journal),
                tool_name,
                arguments if isinstance(arguments, dict) else {},
            )
            # Also check by the parent journal's invocation_id at this index
            parent_idx = len(self._journal)
            parent_inv_id = None
            if parent_idx < len(self._cf_parent_log):
                parent_rec = self._cf_parent_log[parent_idx]
                parent_inv_id = (
                    parent_rec.invocation_id
                    if hasattr(parent_rec, "invocation_id")
                    else None
                )

            for check_id in (pre_inv_id, parent_inv_id):
                if check_id and check_id in self._cf_overrides:
                    override_info = self._cf_overrides[check_id]
                    if isinstance(override_info, tuple):
                        _, override = override_info
                    else:
                        override = override_info

                    # Find original output from parent log
                    original_output = None
                    if parent_idx < len(self._cf_parent_log):
                        original_output = self._cf_parent_log[parent_idx].response

                    cf_rec = build_counterfactual_record(
                        check_id,
                        parent_idx,
                        original_output,
                        override,
                    )
                    self.checkpoint.counterfactual_log.append(cf_rec)
                    if self.checkpoint.diverged_at_step is None:
                        self.checkpoint.diverged_at_step = parent_idx
                    self._past_divergence = True

                    self._append_record(
                        SyscallRecord(
                            request=request,
                            response=override.replacement_output,
                        )
                    )
                    return override.replacement_output

        # ── Lodge eviction hook: run before LLM tools (live execution only) ──
        if (
            self._lodge is not None
            and not self.is_replaying
            and tool_name in self._llm_tool_names
        ):
            await self._lodge.check_and_evict(self, self.checkpoint)

        # ── Gate validation (schema check — Gate concern, not Kernel) ──
        # Spawn/join bypass Gate validation (kernel-internal tools).
        validation_error_response = None
        validated = arguments
        is_spawn_join = tool_name in {"spawn_agent", "spawn_agent_async", "join_agent"}
        if not is_spawn_join:
            try:
                validated = self._gate.validate(tool_name, arguments)
            except ValidationError as e:
                response = self._gate.format_validation_error(tool_name, e)
                validation_error_response = response.model_dump()

        tool_meta = self._gate.get_tool_meta(tool_name) if not is_spawn_join else None

        # ── Kernel security decision (pure function, zero I/O) ──
        # Spawn/join skip Kernel decision — they are Scheduler lifecycle ops
        # with their own replay logic inside _handle_spawn/_handle_join.
        if is_spawn_join:
            # Replay check for spawn/join: serve cached response if available
            idx = self._replay_index
            while idx < len(self._journal):
                rec = self._journal.get(idx)
                if rec.request.get("tool_name") not in self._kernel_tool_names:
                    break
                idx += 1
            if idx < len(self._journal):
                record = self._journal.get(idx)
                if record.request == request:
                    self._replay_index = idx + 1
                    _logger.debug(
                        "replay_hit",
                        extra={
                            "pid": self.checkpoint.pid,
                            "tool": tool_name,
                            "index": idx,
                        },
                    )
                    return self._wrap_if_needed(tool_name, record.response)
            # Not a replay hit — execute the spawn/join
            if tool_name == "spawn_agent":
                return await self._handle_spawn(request, arguments)
            if tool_name == "spawn_agent_async":
                return await self._handle_spawn_async(request, arguments)
            return await self._handle_join(request, arguments)

        decision = decide_syscall(
            journal=self._journal,
            replay_index=self._replay_index,
            kernel_tool_names=self._kernel_tool_names,
            capabilities=self.checkpoint.capabilities,
            request=request,
            tool_meta=tool_meta,
            validated_args=validated,
            validation_error_response=validation_error_response,
            speculative=self._speculative,
        )

        # ── Scheduler executes the Kernel's decision ──
        if isinstance(decision, ReplayHit):
            self._replay_index = decision.new_replay_index
            replayed_idx = decision.new_replay_index - 1
            replayed_record = self._journal.get(replayed_idx)
            inv_id = replayed_record.invocation_id or ""

            _logger.debug(
                "replay_hit",
                extra={
                    "pid": self.checkpoint.pid,
                    "tool": tool_name,
                    "index": replayed_idx,
                },
            )

            # ── Counterfactual override check ──
            # If this syscall has an override, inject it instead of the
            # recorded response. Mark divergence so downstream dispatch
            # follows the CF mode.
            if inv_id and inv_id in self._cf_overrides:
                from castor.scheduler.counterfactual import (
                    build_counterfactual_record,
                )

                override_info = self._cf_overrides[inv_id]
                if isinstance(override_info, tuple):
                    _, override = override_info
                else:
                    override = override_info

                cf_rec = build_counterfactual_record(
                    inv_id,
                    replayed_idx,
                    replayed_record.response,
                    override,
                )
                self.checkpoint.counterfactual_log.append(cf_rec)
                if self.checkpoint.diverged_at_step is None:
                    self.checkpoint.diverged_at_step = replayed_idx
                self._past_divergence = True

                # Record in syscall_log as well (with the override output)
                self._append_record(
                    SyscallRecord(
                        request=request,
                        response=override.replacement_output,
                        purpose=replayed_record.purpose,
                    )
                )
                return self._wrap_if_needed(tool_name, override.replacement_output)

            # ── Post-divergence mode dispatch ──
            if self._past_divergence and self._cf_mode:
                from castor.models.counterfactual import ReplayMode

                if self._cf_mode == ReplayMode.LIVE_FROM_DIVERGENCE:
                    # Don't replay — fall through to live execution below
                    self._replay_index = replayed_idx  # undo advance
                    pass  # fall through
                elif self._cf_mode == ReplayMode.REPLAY_ALL:
                    # Fiction mode: return cached even if args differ
                    return self._wrap_if_needed(tool_name, decision.response)
                # REPLAY_WHEN_ARGS_MATCH: check if args match
                elif self._cf_mode == ReplayMode.REPLAY_WHEN_ARGS_MATCH:
                    if replayed_record.request == request:
                        return self._wrap_if_needed(tool_name, decision.response)
                    # Args differ → fall through to live
                    self._replay_index = replayed_idx
                    pass  # fall through
                else:
                    return self._wrap_if_needed(tool_name, decision.response)

                # Fall-through means: go live (handled by Allow path below)
                # Re-run decide_syscall without replay
                decision = decide_syscall(
                    journal=self._journal,
                    replay_index=len(self._journal),  # past all cached
                    kernel_tool_names=self._kernel_tool_names,
                    capabilities=self.checkpoint.capabilities,
                    request=request,
                    tool_meta=tool_meta,
                    validated_args=validated,
                    validation_error_response=validation_error_response,
                    speculative=self._speculative,
                )
            else:
                # Not past divergence or no CF mode — normal replay
                # Preemption re-injection
                if self.checkpoint.preemption_log:
                    from castor.models.preemption import PreemptedError

                    for prec in self.checkpoint.preemption_log:
                        if prec.syscall_index_after == decision.new_replay_index:
                            self.checkpoint.status = "PREEMPTED"
                            raise PreemptedError(
                                reason=prec.reason,
                                metadata=prec.metadata,
                            )
                return self._wrap_if_needed(tool_name, decision.response)

        if isinstance(decision, Deny):
            self._append_record(
                SyscallRecord(request=request, response=decision.response)
            )
            # Budget-related denial → raise BudgetExhaustedError and lock
            # the checkpoint so all subsequent syscalls are blocked
            # immediately (§2: deterministic immediate preemption).
            resp = decision.response
            is_budget_deny = (
                isinstance(resp, dict)
                and resp.get("status") == "INSUFFICIENT_CAPABILITY"
            )
            if is_budget_deny:
                self.checkpoint.status = "PREEMPTED"
                from castor.models.checkpoint import PreemptionRecord
                from castor.models.preemption import PreemptedError, PreemptionReason

                prec = PreemptionRecord(
                    syscall_index_after=len(self.checkpoint.syscall_log),
                    reason=PreemptionReason.BUDGET_EXHAUSTED,
                    timestamp=time.time(),
                    metadata={
                        "feedback": resp.get("feedback_message", ""),
                    },
                )
                self.checkpoint.preemption_log.append(prec)
                raise PreemptedError(
                    PreemptionReason.BUDGET_EXHAUSTED,
                    metadata=prec.metadata,
                )
            return self._wrap_if_needed(tool_name, decision.response)

        if isinstance(decision, Suspend):
            _hitl_counter.add(1, {"action": "suspend"})
            _logger.info(
                "hitl_suspend",
                extra={
                    "pid": self.checkpoint.pid,
                    "tool": tool_name,
                },
            )
            self.checkpoint.pending_hitl = decision.request
            self.checkpoint.status = "SUSPENDED_FOR_HITL"
            raise SuspendInterrupt(self.checkpoint)

        # ── Allow: deduct budget and execute ──
        assert isinstance(decision, Allow)
        tool_meta = decision.tool_meta
        validated = decision.validated_args
        operation_id = self._make_invocation_id(request)
        execution_arguments = {**validated, "operation_id": operation_id}

        if decision.cost > 0:
            self._budget_mgr.deduct(
                self.checkpoint.capabilities,
                tool_meta.consumes,
                tool_meta.cost_per_use,
            )

        # ── WAL: log intent before execution ──
        # Snapshot captures usage BEFORE deduction so recover() restores correctly.
        wal_syscall_index = len(self._journal)
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
                arguments=execution_arguments,
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
                        loop.run_in_executor(
                            pool,
                            lambda: tool_meta.func(
                                **prepare_execution_arguments(
                                    tool_meta, execution_arguments
                                )
                            ),
                        ),
                        timeout=tool_meta.timeout_seconds,
                    )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
            elif tool_meta.timeout_seconds is not None:
                # Async tool with timeout
                result = await asyncio.wait_for(
                    self._gate.execute(tool_name, execution_arguments),
                    timeout=tool_meta.timeout_seconds,
                )
            else:
                result = await self._gate.execute(tool_name, execution_arguments)
        except BaseException:
            # Abandon WAL entry — tool did not complete successfully
            if self._store is not None:
                self._store.abandon_wal(self.checkpoint.pid, wal_syscall_index)
            # Refund the budget — execution was interrupted (CancelledError from
            # preemption) or failed (tool exception).  Without this, the record
            # is never logged, so replay will re-attempt the syscall and deduct
            # again, causing a permanent budget leak.
            if tool_meta.cost_per_use > 0:
                self._budget_mgr.refund(
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

        # Tag memory syscalls with their purpose for cost accounting.
        purpose = SyscallPurpose.TASK_EXECUTION
        if tool_name in _MEMORY_SYSCALL_NAMES:
            purpose = SyscallPurpose.MEMORY_MANAGEMENT

        # Kernel decided whether this step needs review (via Allow decision)
        self._append_record(
            SyscallRecord(
                request=request,
                response=result,
                needs_review=decision.needs_review,
                review_reason=decision.review_reason,
                purpose=purpose,
            )
        )

        # Post-syscall effects for memory operations.
        if tool_name in _MEMORY_SYSCALL_NAMES and self._lodge is not None:
            await self._apply_memory_effects(
                tool_name, request.get("arguments", {}), result
            )

        # ── Budget overshoot detection ──
        # If the just-completed syscall pushed any budget negative, lock
        # the checkpoint so the NEXT syscall bounces immediately. The
        # current syscall's result is still returned (it already executed).
        if decision.cost > 0 and not self.is_replaying:
            cap = self.checkpoint.capabilities.get(decision.tool_meta.consumes)
            if cap and cap.current_usage > cap.max_budget:
                self.checkpoint.status = "BUDGET_EXHAUSTED"
                _logger.warning(
                    "budget_exhausted",
                    extra={
                        "pid": self.checkpoint.pid,
                        "resource": decision.tool_meta.consumes,
                        "usage": cap.current_usage,
                        "max": cap.max_budget,
                    },
                )

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
        priority: int = arguments.get("priority", 5)

        # 1. Look up agent function
        agent_fn = self._agent_registry.get(agent_name)

        # 2. Delegate capabilities from parent to child
        child_budgets = self._budget_mgr.delegate(
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
            capabilities=child_budgets,
            priority=priority,
        )

        # 5. Run child with its own proxy
        child_kernel_tools = self._lodge.kernel_tool_names if self._lodge else set()
        child_proxy = SyscallProxy(
            checkpoint=child_cp,
            gate=self._gate,
            capability_manager=self._budget_mgr,
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
            self._budget_mgr.reclaim(
                self.checkpoint.capabilities, child_cp.capabilities
            )
            raise

        # 6. Reclaim unused child budget
        self._budget_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)

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
        """Handle spawn_agent_async: delegate caps, launch child task, return handle.

        Children are launched immediately as asyncio tasks. Priority is
        recorded on the child checkpoint for observability and future
        scheduling improvements. In this implementation, all async
        children run concurrently; priority determines join ordering
        when the parent calls ``join_any`` (future) or is used by
        application-level orchestration.
        """
        if self._agent_registry is None:
            raise RuntimeError(
                "spawn_agent_async requires an AgentRegistry on SyscallProxy"
            )

        agent_name: str = arguments["agent_name"]
        requested_caps: dict[str, float] = arguments.get("capabilities", {})
        priority: int = arguments.get("priority", 5)

        # 1. Look up agent function
        agent_fn = self._agent_registry.get(agent_name)

        # 2. Delegate capabilities from parent to child
        child_budgets = self._budget_mgr.delegate(
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
                capabilities=child_budgets,
                priority=priority,
            )

            # 5. Launch child as background task
            child_kernel_tools = self._lodge.kernel_tool_names if self._lodge else set()
            child_proxy = SyscallProxy(
                checkpoint=child_cp,
                gate=self._gate,
                capability_manager=self._budget_mgr,
                lodge=self._lodge,
                llm_tool_names=self._llm_tool_names,
                kernel_tool_names=child_kernel_tools,
                agent_registry=self._agent_registry,
            )

            async def _run_child() -> Any:
                try:
                    from castor.lib._context import set_proxy

                    set_proxy(child_proxy)

                    # Dual-signature: 0 required params = new-style (castor.lib)
                    sig = inspect.signature(agent_fn)
                    required = [
                        p
                        for p in sig.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind
                        not in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        )
                    ]
                    if len(required) == 0:
                        result = await agent_fn()
                    else:
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
            self._budget_mgr.reclaim(self.checkpoint.capabilities, child_budgets)
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
            self._budget_mgr.reclaim(
                self.checkpoint.capabilities, child_cp.capabilities
            )
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
        self._budget_mgr.reclaim(self.checkpoint.capabilities, child_cp.capabilities)

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
            self._budget_mgr.deduct(self.checkpoint.capabilities, resource_type, cost)
        except BudgetExhaustedError:
            # Budget is already exhausted — charge whatever remains.
            cap = self.checkpoint.capabilities.get(resource_type)
            if cap is not None:
                cap.current_usage = cap.max_budget

    async def _apply_memory_effects(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> None:
        """Apply post-syscall side effects for memory operations.

        AISA §2.2 shape — 7 syscalls, ID-based addressing.
        """
        from castor.mmu.core import (
            MEM_DELETE,
            MEM_EVICT,
            MEM_PROMOTE,
            MEM_PROTECT,
            MEM_WRITE,
            MMU,
        )

        mmu = self._lodge
        if not isinstance(mmu, MMU):
            return

        # During replay, effects are already baked into the checkpoint
        # from the original run. Re-applying them would duplicate entries
        # in context_history and cold storage.
        if self.is_replaying:
            return

        r = result if isinstance(result, dict) else {}

        if tool_name == MEM_EVICT:
            mid = arguments.get("memory_id", "")
            if mid:
                removed = mmu.apply_eviction(self.checkpoint, mid)
                if removed:
                    await mmu.persist_evicted(removed)

        elif tool_name == MEM_PROMOTE:
            if r.get("promoted"):
                msg = CastorMessage(
                    id=r.get("memory_id", ""),
                    role=r.get("role", "system"),
                    content=r.get("content", ""),
                )
                mmu.apply_promote(self.checkpoint, msg)

        elif tool_name == MEM_PROTECT:
            mid = arguments.get("memory_id", "")
            protect = arguments.get("protect", True)
            mmu.apply_protect(self.checkpoint, mid, protect)

        elif tool_name == MEM_DELETE:
            mid = arguments.get("memory_id", "")
            mmu.apply_delete(self.checkpoint, mid)
            # cold_storage.delete already called by the tool handler

        elif tool_name == MEM_WRITE:
            content = r.get("content", "")
            pin = r.get("pin", False)
            role = arguments.get("role", "memory")
            mid = mmu.next_memory_id(self.checkpoint.pid, role, content)
            msg = CastorMessage(id=mid, role=role, content=content, pinned=pin)
            mmu.apply_write(self.checkpoint, msg)
            # Also persist to cold for durability
            await mmu._cold.store_explicit(
                mmu._agent_id,
                content,
                metadata=r.get("metadata"),
                memory_id=mid,
            )
            # Patch the result dict so the caller sees the ID
            if isinstance(result, dict):
                result["memory_id"] = mid

        # mem_read and mem_search have no post-syscall effects —
        # they're pure queries.

    def _make_invocation_id(self, request: dict[str, Any]) -> str:
        """Compute a deterministic invocation_id for the next journal entry.

        Uses the current journal length (= next syscall index) so the id
        is stable across replays of the same execution path.
        """
        tool_name = request.get("tool_name", "")
        arguments = request.get("arguments", {})
        return compute_invocation_id(
            pid=self.checkpoint.pid,
            syscall_index=len(self._journal),
            tool_name=tool_name,
            arguments=arguments if isinstance(arguments, dict) else {},
        )

    def _append_record(self, record: SyscallRecord) -> None:
        """Append a record to the journal and advance replay index to stay in sync.

        If the record doesn't have an invocation_id, one is computed
        from its request and the current journal position.

        When a durable ``checkpoint_store`` is configured, persist after each
        committed record so crash recovery can catch up from the journal
        (Paper A: kill-after-success / catch-up prefix determinism).
        """
        if record.invocation_id is None and record.request:
            record.invocation_id = self._make_invocation_id(record.request)
        self._journal.append(record)
        self._replay_index = len(self._journal)
        if self._store is not None:
            self._store.save(self.checkpoint)

    def _wrap_if_needed(self, tool_name: str, response: Any) -> Any:
        """Wrap response in SyscallResult for destructive/HITL tools."""
        if not self._structured_results:
            return response
        if not self._gate.has_tool(tool_name):
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
        self,
        agent_name: str,
        *,
        capabilities: dict[str, float] | None = None,
        priority: int = 5,
    ) -> str:
        """Spawn a child agent asynchronously and return a join handle.

        Sugar for ``proxy.syscall("spawn_agent_async", ...)``.
        ``priority`` (1-10, default 5) determines dispatch order when
        multiple children compete for resources.
        """
        return await self.syscall(
            "spawn_agent_async",
            agent_name=agent_name,
            capabilities=capabilities or {},
            priority=priority,
        )

    async def join(self, handle: str) -> Any:
        """Wait for a spawned child agent to complete and return its result.

        Sugar for ``proxy.syscall("join_agent", ...)``.
        """
        return await self.syscall("join_agent", handle=handle)

    async def spawn_sync(
        self,
        agent_name: str,
        *,
        capabilities: dict[str, float] | None = None,
        priority: int = 5,
    ) -> Any:
        """Spawn a child agent synchronously and wait for its result.

        Sugar for ``proxy.syscall("spawn_agent", ...)``.
        """
        return await self.syscall(
            "spawn_agent",
            agent_name=agent_name,
            capabilities=capabilities or {},
            priority=priority,
        )

    # ── Priority-based dispatch queue ──

    def enqueue_spawn(
        self,
        child_pid: str,
        agent_fn: Any,
        child_cp: AgentCheckpoint,
    ) -> None:
        """Add a child to the priority dispatch queue.

        Children are dispatched in priority order (highest first, then
        FIFO among same-priority). Use ``dispatch_next()`` to pop and
        run the next child.
        """
        import heapq

        heapq.heappush(
            self._spawn_queue,
            (-child_cp.priority, self._spawn_queue_seq, child_pid, agent_fn, child_cp),
        )
        self._spawn_queue_seq += 1

    def dispatch_next(self) -> tuple[str, Any, AgentCheckpoint] | None:
        """Pop the highest-priority child from the queue.

        Returns ``(child_pid, agent_fn, child_cp)`` or ``None`` if
        queue is empty.
        """
        import heapq

        if not self._spawn_queue:
            return None
        neg_pri, _seq, child_pid, agent_fn, child_cp = heapq.heappop(self._spawn_queue)
        return (child_pid, agent_fn, child_cp)

    @property
    def spawn_queue_size(self) -> int:
        """Number of children waiting in the dispatch queue."""
        return len(self._spawn_queue)

    def __getattr__(self, name: str) -> Any:
        """Enable proxy.tool_name(...) style calls.

        Returns an async callable that delegates to syscall().
        Only triggers for names not found via normal attribute lookup.
        """
        # Check if it's a registered tool
        if self._gate.has_tool(name):

            async def _tool_call(**kwargs: Any) -> Any:
                return await self.syscall(name, **kwargs)

            return _tool_call

        raise AttributeError(
            f"'{type(self).__name__}' has no attribute '{name}' "
            f"and '{name}' is not a registered tool"
        )
