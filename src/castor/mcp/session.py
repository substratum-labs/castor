"""MCP session state management: budgets and pending HITL requests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from castor.models.budget import Budget

STATE_KEY = "castor_session"


class HITLRequest(BaseModel):
    """A pending HITL approval request."""

    request_id: str
    tool_name: str
    arguments: dict[str, Any]
    resource: str
    cost: float


class SessionState(BaseModel):
    """Per-MCP-session state: budgets + pending HITL queue."""

    initialized: bool = False
    capabilities: dict[str, Budget] = {}
    pending_hitl: dict[str, HITLRequest] = {}
    audit_log: list[dict[str, Any]] = []


async def load_state(ctx: Any) -> SessionState:
    """Load session state from FastMCP context, or return default."""
    raw = await ctx.get_state(STATE_KEY)
    if raw is None:
        return SessionState()
    return SessionState.model_validate(raw)


async def save_state(ctx: Any, state: SessionState) -> None:
    """Persist session state to FastMCP context."""
    await ctx.set_state(STATE_KEY, state.model_dump())
