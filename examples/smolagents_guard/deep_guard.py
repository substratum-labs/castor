"""CastorResilientAgent — Level 2 smolagents integration.

Adds checkpoint/replay and HITL suspend/resume to smolagents.
Builds on Level 1 (guard.py) by intercepting both LLM calls and tool calls,
recording them in Castor's native SyscallRecord log.
"""

from __future__ import annotations

from typing import Any

from smolagents import ToolCallingAgent
from smolagents.models import ChatMessage, Model

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.stream.persistence import CheckpointStore

# ── ChatMessage serialization ──


def _serialize_chat_message(msg: ChatMessage) -> dict[str, Any]:
    """Convert ChatMessage to a JSON-safe dict for SyscallRecord.response."""
    return msg.dict()


def _deserialize_chat_message(data: dict[str, Any]) -> ChatMessage:
    """Reconstruct ChatMessage from a stored dict."""
    return ChatMessage.from_dict(data)


# ── ReplayModel ──


class ReplayModel(Model):
    """Wraps a smolagents Model to record/replay LLM calls via syscall_log."""

    def __init__(self, inner: Model, agent: CastorResilientAgent):
        super().__init__(model_id=f"castor-replay:{inner.model_id}")
        self._inner = inner
        self._agent = agent

    def generate(self, messages, **kwargs):
        request = {"tool_name": "__llm__", "arguments": {"n_messages": len(messages)}}

        if self._agent.is_replaying:
            data = self._agent._advance_replay(request)
            return _deserialize_chat_message(data)

        result = self._inner.generate(messages, **kwargs)
        self._agent._record(request, _serialize_chat_message(result))
        return result


# ── Errors ──


class HITLSuspendError(Exception):
    """Agent suspended for HITL approval. Checkpoint saved to store."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
        super().__init__(f"Suspended for HITL: {checkpoint.pending_hitl}")


class ReplayDivergenceError(Exception):
    """Raised when a replay request doesn't match the recorded syscall."""

    def __init__(self, index: int, expected: dict[str, Any], actual: dict[str, Any]):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Replay divergence at index {index}: expected {expected}, got {actual}"
        )


# ── CastorResilientAgent ──


class CastorResilientAgent(ToolCallingAgent):
    """Level 2 smolagents integration: budget + HITL + checkpoint/replay.

    Args:
        tools: List of smolagents Tool instances.
        model: The LLM model to use (will be wrapped with ReplayModel).
        budgets: Resource budgets, e.g. ``{"network": 20.0, "disk": 10.0}``.
        tool_policies: Per-tool policy dict.
        checkpoint_store: Optional CheckpointStore for SQLite persistence.
        checkpoint: Optional existing checkpoint to resume from.
        hitl_approved_request: The request that was approved via HITL (for resume).
        pid: Agent process ID (default: "smolagent-001").
        **kwargs: Passed to ``ToolCallingAgent.__init__``.
    """

    def __init__(
        self,
        tools,
        model,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        checkpoint_store: CheckpointStore | None = None,
        checkpoint: AgentCheckpoint | None = None,
        hitl_approved_request: dict[str, Any] | None = None,
        pid: str = "smolagent-001",
        **kwargs,
    ):
        # Create or restore checkpoint
        self.cap_mgr = CapabilityManager()
        if checkpoint is not None:
            self._checkpoint = checkpoint
            # Reset capability usage — replay will re-deduct each recorded
            # syscall's cost, rebuilding the correct totals from scratch.
            # This mirrors a real restore from storage where capabilities
            # are deserialized fresh.
            for cap in checkpoint.capabilities.values():
                cap.current_usage = 0.0
            self.capabilities = checkpoint.capabilities
        else:
            self.capabilities = self.cap_mgr.create_capabilities(budgets)
            self._checkpoint = AgentCheckpoint(
                pid=pid,
                status="RUNNING",
                agent_function_name="smolagents_resilient",
                capabilities=self.capabilities,
            )

        self.tool_policies = tool_policies
        self._store = checkpoint_store
        self._replay_index = 0
        self._hitl_approved_request = hitl_approved_request
        self.audit_log: list[dict[str, Any]] = []

        # Wrap model with ReplayModel BEFORE passing to super().__init__
        replay_model = ReplayModel(model, self)
        super().__init__(tools=tools, model=replay_model, **kwargs)

    @property
    def is_replaying(self) -> bool:
        return self._replay_index < len(self._checkpoint.syscall_log)

    def _advance_replay(self, request: dict[str, Any]) -> Any:
        """Serve cached response from syscall_log."""
        record = self._checkpoint.syscall_log[self._replay_index]
        if record.request != request:
            raise ReplayDivergenceError(self._replay_index, record.request, request)
        self._replay_index += 1
        return record.response

    def _record(self, request: dict[str, Any], response: Any) -> None:
        """Append new syscall record and persist checkpoint.

        Also advances ``_replay_index`` to stay past the end of the log,
        preventing ``is_replaying`` from becoming True mid-session.
        """
        self._checkpoint.syscall_log.append(
            SyscallRecord(request=request, response=response)
        )
        self._replay_index = len(self._checkpoint.syscall_log)
        self._save_checkpoint()

    def _save_checkpoint(self) -> None:
        if self._store is not None:
            self._store.save(self._checkpoint)

    def _replay_budget(self, tool_name: str) -> None:
        """Re-deduct budget during replay to keep capabilities consistent."""
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        request = {"tool_name": tool_name, "arguments": arguments}

        # ── Replay path ──
        if self.is_replaying:
            result = self._advance_replay(request)
            self._replay_budget(tool_name)
            self.audit_log.append({"tool": tool_name, "replayed": True})
            return result

        # ── Live path ──
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — suspend, don't block
        if policy.get("destructive", False):
            # Check if this was the HITL-approved call (resume scenario)
            if self._hitl_approved_request == request:
                self._hitl_approved_request = None  # consume it
            else:
                self._checkpoint.pending_hitl = request
                self._checkpoint.status = "SUSPENDED_FOR_HITL"
                self._save_checkpoint()
                raise HITLSuspendError(self._checkpoint)

        # 3. Execute via smolagents
        result = super().execute_tool_call(tool_name, arguments)

        # 4. Record + audit
        self._record(request, result)
        self.audit_log.append(
            {
                "tool": tool_name,
                "cost": cost,
                "resource": resource,
                "replayed": False,
            }
        )
        return result

    def budget_summary(self) -> dict[str, dict[str, float]]:
        """Return current budget usage for display."""
        return {
            name: {
                "used": cap.current_usage,
                "max": cap.max_budget,
                "remaining": cap.max_budget - cap.current_usage,
            }
            for name, cap in self.capabilities.items()
        }
