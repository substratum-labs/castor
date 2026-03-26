"""Tests for kernel security decisions — pure function, no I/O."""

from __future__ import annotations

import pytest

from castor.gate.registry import ToolMetadata
from castor.kernel.decisions import (
    Allow,
    Deny,
    ReplayDivergenceError,
    ReplayHit,
    Suspend,
    decide_syscall,
)
from castor.models.capability import Capability
from castor.models.checkpoint import SyscallRecord


def _meta(
    *,
    name: str = "test_tool",
    cost: float = 1.0,
    destructive: bool = False,
    requires_hitl: bool = False,
) -> ToolMetadata:
    return ToolMetadata(
        tool_name=name,
        func=lambda: None,
        input_schema={},
        consumes="api",
        cost_per_use=cost,
        destructive=destructive,
        requires_hitl=requires_hitl,
    )


def _caps(budget: float = 100.0, usage: float = 0.0) -> dict[str, Capability]:
    return {
        "api": Capability(resource_type="api", max_budget=budget, current_usage=usage)
    }


def _request(tool: str = "test_tool", args: dict | None = None) -> dict:
    return {"tool_name": tool, "arguments": args or {}}


def _record(tool: str = "test_tool", args: dict | None = None, response: str = "ok"):
    return SyscallRecord(
        request={"tool_name": tool, "arguments": args or {}},
        response=response,
    )


class TestReplay:
    def test_replay_hit(self):
        log = [_record("test_tool", response="cached")]
        decision = decide_syscall(
            syscall_log=log,
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, ReplayHit)
        assert decision.response == "cached"
        assert decision.new_replay_index == 1

    def test_replay_divergence(self):
        log = [_record("other_tool", response="cached")]
        with pytest.raises(ReplayDivergenceError) as exc_info:
            decide_syscall(
                syscall_log=log,
                replay_index=0,
                kernel_tool_names=set(),
                capabilities=_caps(),
                request=_request("test_tool"),
                tool_meta=_meta(),
                validated_args={},
                validation_error_response=None,
            )
        assert exc_info.value.index == 0

    def test_replay_skips_kernel_tools(self):
        log = [
            _record("sys_kernel_page_out", response="evicted"),
            _record("test_tool", response="cached"),
        ]
        decision = decide_syscall(
            syscall_log=log,
            replay_index=0,
            kernel_tool_names={"sys_kernel_page_out"},
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, ReplayHit)
        assert decision.response == "cached"
        assert decision.new_replay_index == 2

    def test_no_replay_when_past_log(self):
        """Past end of log → not a replay, proceed to security checks."""
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Allow)


class TestValidation:
    def test_validation_error_returns_deny(self):
        error_resp = {"status": "VALIDATION_ERROR", "feedback_message": "bad args"}
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(),
            validated_args=None,
            validation_error_response=error_resp,
        )
        assert isinstance(decision, Deny)
        assert decision.response["status"] == "VALIDATION_ERROR"


class TestHITL:
    def test_requires_hitl_suspends(self):
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(requires_hitl=True),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Suspend)

    def test_destructive_zero_cost_suspends(self):
        """Destructive tools with no budget tracking always need HITL."""
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(),
            request=_request(),
            tool_meta=_meta(destructive=True, cost=0.0),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Suspend)


class TestBudget:
    def test_budget_sufficient_allows(self):
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(budget=100.0, usage=0.0),
            request=_request(),
            tool_meta=_meta(cost=1.0),
            validated_args={"x": 1},
            validation_error_response=None,
        )
        assert isinstance(decision, Allow)
        assert decision.cost == 1.0
        assert decision.validated_args == {"x": 1}

    def test_budget_exhausted_non_destructive_denies(self):
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(budget=10.0, usage=10.0),
            request=_request(),
            tool_meta=_meta(cost=1.0, destructive=False),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Deny)
        assert decision.response["status"] == "INSUFFICIENT_CAPABILITY"

    def test_budget_exhausted_destructive_suspends(self):
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(budget=10.0, usage=10.0),
            request=_request(),
            tool_meta=_meta(cost=1.0, destructive=True),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Suspend)

    def test_zero_cost_skips_budget_check(self):
        """Zero-cost tools are always allowed regardless of budget."""
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities=_caps(budget=0.0, usage=0.0),
            request=_request(),
            tool_meta=_meta(cost=0.0),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Allow)
        assert decision.cost == 0.0

    def test_untracked_resource_allows(self):
        """Missing resource type in capabilities = not tracked = unlimited."""
        decision = decide_syscall(
            syscall_log=[],
            replay_index=0,
            kernel_tool_names=set(),
            capabilities={},  # no "api" capability
            request=_request(),
            tool_meta=_meta(cost=1.0),
            validated_args={},
            validation_error_response=None,
        )
        assert isinstance(decision, Allow)
