"""Castor MCP Server: expose @castor_tool functions as MCP tools with budget + HITL."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from fastmcp import FastMCP
from fastmcp.server.context import Context, _current_context
from fastmcp.tools.tool import Tool, ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import ValidationError

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolMetadata, ToolRegistry, default_registry
from castor.gate.validator import SyscallGate
from castor.mcp.session import HITLRequest, load_state, save_state


class CastorMCPTool(Tool):
    """Wraps a Castor ToolMetadata as an MCP tool.

    Routes calls through SyscallGate for validation and execution,
    with budget enforcement and HITL gating.
    """

    KEY_PREFIX: ClassVar[str] = "tool"

    consumes: str = "_default"
    cost_per_use: float = 0.0
    requires_hitl_approval: bool = False
    is_destructive: bool = False

    @classmethod
    def from_castor_meta(cls, meta: ToolMetadata) -> CastorMCPTool:
        """Create an MCP tool from Castor ToolMetadata."""
        annotations = ToolAnnotations(
            readOnlyHint=not meta.destructive,
            destructiveHint=meta.destructive,
        )

        return cls(
            name=meta.tool_name,
            description=_get_description(meta),
            parameters=meta.input_schema,
            annotations=annotations,
            consumes=meta.consumes,
            cost_per_use=meta.cost_per_use,
            requires_hitl_approval=meta.requires_hitl,
            is_destructive=meta.destructive,
            timeout=meta.timeout_seconds,
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Execute tool with Castor budget + HITL enforcement."""
        ctx = _current_context.get()
        if ctx is None:
            return _text_result("Error: No MCP context available.")

        gate: SyscallGate = ctx.lifespan_context["gate"]
        budget_mgr: BudgetManager = ctx.lifespan_context["budget_mgr"]
        state = await load_state(ctx)

        # 1. Require session initialization
        if not state.initialized:
            return _text_result(
                "Session not initialized. "
                "Call castor_init(budgets={...}) first to set resource budgets."
            )

        # 2. Validate arguments via SyscallGate
        try:
            validated = gate.validate(self.name, arguments)
        except ValidationError as e:
            resp = gate.format_validation_error(self.name, e)
            return _text_result(resp.feedback_message)

        # 3. Budget enforcement
        if self.cost_per_use > 0:
            if not budget_mgr.check(
                state.capabilities, self.consumes, self.cost_per_use
            ):
                cap = state.capabilities.get(self.consumes)
                remaining = (cap.max_budget - cap.current_usage) if cap else 0.0
                await save_state(ctx, state)
                return _text_result(
                    f"Budget exhausted for '{self.consumes}': "
                    f"need {self.cost_per_use}, remaining {remaining:.2f}."
                )
            budget_mgr.deduct(state.capabilities, self.consumes, self.cost_per_use)

        # 4. HITL gate for destructive/requires_hitl tools
        if self.requires_hitl_approval or self.is_destructive:
            request_id = str(uuid.uuid4())
            state.pending_hitl[request_id] = HITLRequest(
                request_id=request_id,
                tool_name=self.name,
                arguments=validated,
                resource=self.consumes,
                cost=self.cost_per_use,
            )
            await save_state(ctx, state)
            return _text_result(
                json.dumps(
                    {
                        "status": "pending_approval",
                        "request_id": request_id,
                        "tool_name": self.name,
                        "arguments": validated,
                        "message": (
                            f"Tool '{self.name}' requires approval. "
                            f"castor_approve('{request_id}'), "
                            f"castor_reject('{request_id}'), or "
                            f"castor_modify('{request_id}', feedback)"
                        ),
                    }
                )
            )

        # 5. Execute tool
        try:
            result = await gate.execute(self.name, validated)
        except Exception as e:
            budget_mgr.refund(state.capabilities, self.consumes, self.cost_per_use)
            await save_state(ctx, state)
            return _text_result(f"Tool execution failed: {e}")

        # 6. Audit log
        state.audit_log.append(
            {
                "tool": self.name,
                "cost": self.cost_per_use,
                "resource": self.consumes,
            }
        )
        await save_state(ctx, state)

        # 7. Return result
        return self.convert_result(result)


def _get_description(meta: ToolMetadata) -> str:
    """Generate MCP tool description from Castor ToolMetadata."""
    desc = ""
    if meta.func and meta.func.__doc__:
        desc = meta.func.__doc__.strip()

    hints = []
    if meta.cost_per_use > 0:
        hints.append(f"Cost: {meta.cost_per_use} {meta.consumes}")
    if meta.destructive:
        hints.append("DESTRUCTIVE - requires human approval")
    elif meta.requires_hitl:
        hints.append("Requires human approval")

    if hints:
        if desc:
            desc += "\n\n"
        desc += f"[Castor: {', '.join(hints)}]"

    return desc or meta.tool_name


def _text_result(text: str) -> ToolResult:
    """Create a simple text ToolResult."""
    return ToolResult(content=[TextContent(type="text", text=text)])


# ── Meta-tools ──────────────────────────────────────────────────────────────


def _register_meta_tools(server: FastMCP) -> None:
    """Register Castor control-plane tools on the FastMCP server."""

    @server.tool(
        name="castor_init",
        description=(
            "Initialize Castor security budgets for this session. "
            "Must be called before using any Castor tools. "
            "Example: castor_init(budgets={'api': 50.0, 'disk': 10.0})"
        ),
    )
    async def castor_init(budgets: dict[str, float], ctx: Context) -> str:
        state = await load_state(ctx)
        if state.initialized:
            return "Session already initialized. Budgets cannot be changed mid-session."

        budget_mgr: BudgetManager = ctx.lifespan_context["budget_mgr"]
        state.capabilities = budget_mgr.create_budgets(budgets)
        state.initialized = True
        await save_state(ctx, state)

        summary = ", ".join(f"{k}: {v:.1f}" for k, v in budgets.items())
        return f"Castor session initialized. Budgets: {summary}"

    @server.tool(
        name="castor_status",
        description=(
            "Show current Castor session status: budget usage and pending approvals."
        ),
    )
    async def castor_status(ctx: Context) -> str:
        state = await load_state(ctx)
        if not state.initialized:
            return "Session not initialized. Call castor_init first."

        lines = ["=== Castor Session Status ===", "", "Budgets:"]
        for name, cap in state.capabilities.items():
            remaining = cap.max_budget - cap.current_usage
            lines.append(f"  {name}: {remaining:.2f} / {cap.max_budget:.2f} remaining")

        if state.pending_hitl:
            lines.append(f"\nPending approvals ({len(state.pending_hitl)}):")
            for req_id, req in state.pending_hitl.items():
                lines.append(f"  [{req_id[:8]}] {req.tool_name}({req.arguments})")
        else:
            lines.append("\nNo pending approvals.")

        lines.append(f"\nTotal tool calls: {len(state.audit_log)}")
        return "\n".join(lines)

    @server.tool(
        name="castor_approve",
        description=(
            "Approve and execute a pending tool call that required human approval."
        ),
    )
    async def castor_approve(request_id: str, ctx: Context) -> str:
        state = await load_state(ctx)
        if request_id not in state.pending_hitl:
            return f"No pending request with ID '{request_id}'."

        req = state.pending_hitl.pop(request_id)
        gate: SyscallGate = ctx.lifespan_context["gate"]
        budget_mgr: BudgetManager = ctx.lifespan_context["budget_mgr"]

        try:
            result = await gate.execute(req.tool_name, req.arguments)
        except Exception as e:
            budget_mgr.refund(state.capabilities, req.resource, req.cost)
            await save_state(ctx, state)
            return f"Execution failed: {e}. Budget refunded."

        state.audit_log.append(
            {
                "tool": req.tool_name,
                "cost": req.cost,
                "resource": req.resource,
                "hitl": "approved",
            }
        )
        await save_state(ctx, state)
        return json.dumps({"status": "approved", "result": result}, default=str)

    @server.tool(
        name="castor_reject",
        description="Reject a pending tool call and refund the budget.",
    )
    async def castor_reject(
        request_id: str, reason: str = "Rejected by user", *, ctx: Context
    ) -> str:
        state = await load_state(ctx)
        if request_id not in state.pending_hitl:
            return f"No pending request with ID '{request_id}'."

        req = state.pending_hitl.pop(request_id)
        budget_mgr: BudgetManager = ctx.lifespan_context["budget_mgr"]
        budget_mgr.refund(state.capabilities, req.resource, req.cost)

        state.audit_log.append(
            {"tool": req.tool_name, "hitl": "rejected", "reason": reason}
        )
        await save_state(ctx, state)
        return json.dumps(
            {"status": "rejected", "tool_name": req.tool_name, "reason": reason}
        )

    @server.tool(
        name="castor_modify",
        description=(
            "Reject a pending tool call with modification feedback. "
            "The budget is refunded and feedback is returned for re-planning."
        ),
    )
    async def castor_modify(request_id: str, feedback: str, ctx: Context) -> str:
        state = await load_state(ctx)
        if request_id not in state.pending_hitl:
            return f"No pending request with ID '{request_id}'."

        req = state.pending_hitl.pop(request_id)
        budget_mgr: BudgetManager = ctx.lifespan_context["budget_mgr"]
        budget_mgr.refund(state.capabilities, req.resource, req.cost)

        state.audit_log.append(
            {"tool": req.tool_name, "hitl": "modified", "feedback": feedback}
        )
        await save_state(ctx, state)
        return json.dumps(
            {
                "status": "modified",
                "tool_name": req.tool_name,
                "original_args": req.arguments,
                "feedback": feedback,
                "message": (
                    f"Please re-plan the '{req.tool_name}' call "
                    f"incorporating this feedback: {feedback}"
                ),
            }
        )


# ── Server factory ──────────────────────────────────────────────────────────


def create_mcp_server(
    *,
    tools: list[Callable] | None = None,
    registry: ToolRegistry | None = None,
    gate: SyscallGate | None = None,
    name: str = "Castor MCP Server",
    instructions: str | None = None,
) -> FastMCP:
    """Create a FastMCP server exposing Castor tools with budget + HITL.

    Args:
        tools: List of @castor_tool decorated functions.
        registry: Existing ToolRegistry. Mutually exclusive with tools.
        gate: Existing SyscallGate. If provided, registry/tools are ignored.
        name: MCP server name.
        instructions: System instructions for MCP clients.

    Returns:
        Configured FastMCP server instance.
    """
    if gate is not None:
        _gate = gate
    elif registry is not None:
        _gate = SyscallGate(registry)
    elif tools is not None:
        _registry = ToolRegistry()
        for fn in tools:
            meta = getattr(fn, "_castor_metadata", None)
            if meta is None:
                msg = f"{fn!r} is not a @castor_tool"
                raise TypeError(msg)
            _registry.register(meta)
        _gate = SyscallGate(_registry)
    else:
        _gate = SyscallGate(default_registry)

    _budget_mgr = BudgetManager()

    if instructions is None:
        tool_names = _gate.list_tools()
        instructions = (
            "This server provides tools guarded by Castor security.\n"
            "1. Call castor_init(budgets={...}) first to set resource budgets.\n"
            "2. Use tools normally — budget is auto-deducted.\n"
            "3. Destructive tools return 'pending_approval' — use "
            "castor_approve/castor_reject/castor_modify to resolve.\n"
            "4. Call castor_status() to check remaining budgets.\n"
            f"\nAvailable Castor tools: {', '.join(tool_names)}"
        )

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        yield {"gate": _gate, "budget_mgr": _budget_mgr}

    server = FastMCP(name=name, instructions=instructions, lifespan=lifespan)

    # Register Castor tools as MCP tools
    for tool_name in _gate.list_tools():
        meta = _gate.get_tool_meta(tool_name)
        mcp_tool = CastorMCPTool.from_castor_meta(meta)
        server.add_tool(mcp_tool)

    # Register meta-tools
    _register_meta_tools(server)

    return server


# ── CLI entry point ─────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for the Castor MCP server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="castor-mcp",
        description="Start a Castor MCP server that exposes registered tools",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transports (default: 8000)",
    )
    parser.add_argument(
        "--tools-module",
        type=str,
        default=None,
        help="Python module to import (triggers @castor_tool registration)",
    )

    args = parser.parse_args()

    if args.tools_module:
        import importlib

        importlib.import_module(args.tools_module)

    server = create_mcp_server()
    server.run(transport=args.transport)
