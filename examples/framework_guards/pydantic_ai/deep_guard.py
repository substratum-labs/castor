"""Level 2: CastorResilientToolset — checkpoint/replay + HITL suspend/resume.

Adds crash recovery and non-blocking HITL to the Level 1 guard.
Both tool calls and LLM calls are recorded to a syscall log.
On resume, cached responses are served (zero real API calls during replay).

Usage:
    from pydantic_ai.deep_guard import CastorResilientToolset, ReplayModel

    inner = FunctionToolset([fetch_price, execute_trade])
    resilient = CastorResilientToolset(
        wrapped=inner,
        budgets={"api_calls": 5.0},
        tool_policies={...},
    )
    replay_model = ReplayModel(inner_model=real_model, journal=resilient.journal)
    agent = Agent(replay_model, toolsets=[resilient])
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai._run_context import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from castor.budget.manager import BudgetManager
from castor.models.budget import Capability
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.scheduler.persistence import CheckpointStore

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HITLSuspendError(Exception):
    """Agent suspended for HITL approval. Checkpoint saved to store."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
        super().__init__(
            f"Agent suspended: {checkpoint.pending_tool}({checkpoint.pending_args})"
        )


class ReplayDivergenceError(Exception):
    """Replay request doesn't match recorded syscall."""

    def __init__(self, index: int, expected: dict[str, Any], actual: dict[str, Any]):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Replay divergence at index {index}: expected {expected!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# ModelResponse serialization helpers
# ---------------------------------------------------------------------------


def _serialize_response(resp: ModelResponse) -> dict[str, Any]:
    """Convert ModelResponse to a JSON-safe dict for the syscall log."""
    return dataclasses.asdict(resp)


def _deserialize_response(data: dict[str, Any]) -> ModelResponse:
    """Reconstruct ModelResponse from a stored dict."""
    parts: list[ModelResponsePart] = []
    for part_data in data.get("parts", []):
        kind = part_data.get("part_kind", "text")
        if kind == "text":
            parts.append(TextPart(content=part_data["content"]))
        elif kind == "tool-call":
            parts.append(
                ToolCallPart(
                    tool_name=part_data["tool_name"],
                    args=part_data.get("args"),
                    tool_call_id=part_data.get("tool_call_id", ""),
                )
            )
    return ModelResponse(parts=parts)


# ---------------------------------------------------------------------------
# SyscallJournal — shared mutable state for replay coordination
# ---------------------------------------------------------------------------


class SyscallJournal:
    """Shared state between ReplayModel and CastorResilientToolset.

    Both LLM calls and tool calls are recorded to the same journal.
    This object is shared by reference, so dataclasses.replace() on the
    toolset does NOT create a disconnected copy.
    """

    def __init__(
        self,
        checkpoint: AgentCheckpoint,
        store: CheckpointStore | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self.store = store
        self.replay_index = 0

    @property
    def is_replaying(self) -> bool:
        return self.replay_index < len(self.checkpoint.syscall_log)

    def advance_replay(self, request: dict[str, Any]) -> Any:
        """Serve cached response from syscall_log."""
        record = self.checkpoint.syscall_log[self.replay_index]
        if record.request != request:
            raise ReplayDivergenceError(self.replay_index, record.request, request)
        self.replay_index += 1
        return record.response

    def record(self, request: dict[str, Any], response: Any) -> None:
        """Append new syscall record and advance replay index."""
        self.checkpoint.syscall_log.append(
            SyscallRecord(request=request, response=response)
        )
        # CRITICAL: Advance replay_index past end to prevent
        # is_replaying from retroactively becoming True.
        self.replay_index = len(self.checkpoint.syscall_log)
        self._save()

    def _save(self) -> None:
        if self.store is not None:
            self.store.save(self.checkpoint)


# ---------------------------------------------------------------------------
# ReplayModel — wraps pydantic-ai's Model for LLM call recording
# ---------------------------------------------------------------------------


class ReplayModel(Model):
    """Wraps a pydantic-ai Model to record/replay LLM calls.

    During live execution, delegates to the inner model and records
    the response. During replay, serves cached responses without
    calling the inner model.
    """

    def __init__(self, inner_model: Model, journal: SyscallJournal):
        self._inner = inner_model
        self._journal = journal
        self.call_count = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        request = {"tool_name": "__llm__", "arguments": {"n_messages": len(messages)}}

        # Replay path: serve cached response
        if self._journal.is_replaying:
            data = self._journal.advance_replay(request)
            return _deserialize_response(data)

        # Live path: call inner model + record
        self.call_count += 1
        result = await self._inner.request(
            messages, model_settings, model_request_parameters
        )
        self._journal.record(request, _serialize_response(result))
        return result

    @property
    def model_name(self) -> str:
        return f"castor-replay:{self._inner.model_name}"

    @property
    def system(self) -> str:
        return self._inner.system


# ---------------------------------------------------------------------------
# CastorResilientToolset — L2 deep integration
# ---------------------------------------------------------------------------


@dataclass
class CastorResilientToolset(WrapperToolset[Any]):
    """Wraps a pydantic-ai toolset with Castor checkpoint/replay + HITL.

    Extends the Level 1 guard with:
    - Syscall logging: every tool call and LLM call is recorded.
    - Crash recovery: resume from checkpoint replays cached responses.
    - HITL suspend/resume: destructive tools trigger suspension (not blocking).
    """

    budgets: dict[str, float] = field(default_factory=dict)
    tool_policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    checkpoint: AgentCheckpoint | None = None
    checkpoint_store: CheckpointStore | None = None
    hitl_approved_request: dict[str, Any] | None = None
    pid: str = "pydantic-agent-001"

    # Computed fields
    budget_mgr: BudgetManager = field(init=False, repr=False)
    capabilities: dict[str, Capability] = field(init=False, repr=False)
    audit_log: list[dict[str, Any]] = field(
        init=False, default_factory=list, repr=False
    )
    journal: SyscallJournal = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.budget_mgr = BudgetManager()

        if self.checkpoint is not None:
            cp = self.checkpoint
            # CRITICAL: Reset capabilities to 0.0 on restore.
            # Replay will re-deduct each recorded syscall's cost.
            for cap in cp.capabilities.values():
                cap.current_usage = 0.0
            self.capabilities = cp.capabilities
        else:
            self.capabilities = self.budget_mgr.create_budgets(self.budgets)
            cp = AgentCheckpoint(
                pid=self.pid,
                status="RUNNING",
                agent_function_name="pydantic_ai_resilient",
                capabilities=self.capabilities,
            )

        self.journal = SyscallJournal(checkpoint=cp, store=self.checkpoint_store)

    def _replay_budget(self, tool_name: str) -> None:
        """Re-deduct budget during replay for consistency."""
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)
        if resource:
            self.budget_mgr.deduct(self.capabilities, resource, cost)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        request = {"tool_name": name, "arguments": tool_args}

        # ── Replay path ──
        if self.journal.is_replaying:
            result = self.journal.advance_replay(request)
            self._replay_budget(name)
            self.audit_log.append({"tool": name, "replayed": True})
            return result

        # ── Live path ──
        policy = self.tool_policies.get(name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            self.budget_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — suspend, don't block
        if policy.get("destructive", False):
            if self.hitl_approved_request == request:
                self.hitl_approved_request = None  # consume approval
            else:
                self.journal.checkpoint.pending_hitl = request
                self.journal.checkpoint.status = "SUSPENDED_FOR_HITL"
                self.journal._save()
                raise HITLSuspendError(self.journal.checkpoint)

        # 3. Execute via wrapped toolset
        result = await super().call_tool(name, tool_args, ctx, tool)

        # 4. Record + audit
        self.journal.record(request, result)
        self.audit_log.append(
            {"tool": name, "cost": cost, "resource": resource, "replayed": False}
        )
        return result

    def visit_and_replace(
        self,
        visitor: Callable[[AbstractToolset[Any]], AbstractToolset[Any]],
    ) -> AbstractToolset[Any]:
        """Preserve mutable state across dataclass copies."""
        new_wrapped = self.wrapped.visit_and_replace(visitor)
        result = replace(self, wrapped=new_wrapped)
        # Share mutable references — journal, capabilities, audit_log
        result.budget_mgr = self.budget_mgr
        result.capabilities = self.capabilities
        result.audit_log = self.audit_log
        result.journal = self.journal
        result.hitl_approved_request = self.hitl_approved_request
        return result

    def budget_summary(self) -> dict[str, dict[str, float]]:
        """Return current budget usage for display."""
        cp = self.journal.checkpoint
        return {
            name: {
                "used": cp.budget_used(name),
                "max": cp.budget_used(name) + cp.budget_remaining(name),
                "remaining": cp.budget_remaining(name),
            }
            for name in self.capabilities
        }
