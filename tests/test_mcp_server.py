"""Tests for the Castor MCP Server."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolRegistry
from castor.mcp.server import create_mcp_server

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def registry():
    """Create a test registry with sample tools."""
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    def search(query: str) -> list[str]:
        """Search for documents matching the query."""
        return [f"Result for '{query}'"]

    @castor_tool(
        consumes="api",
        cost_per_use=2.0,
        destructive=True,
        registry=reg,
    )
    def delete_item(item_id: str) -> str:
        """Delete an item by ID."""
        return f"Deleted {item_id}"

    @castor_tool(
        consumes="api",
        cost_per_use=1.5,
        requires_hitl=True,
        registry=reg,
    )
    async def send_email(to: str, body: str) -> str:
        """Send an email."""
        return f"Sent to {to}"

    @castor_tool(consumes="api", cost_per_use=0.0, registry=reg)
    def free_tool() -> str:
        """A free tool with no cost."""
        return "free result"

    return reg


@pytest.fixture()
def server(registry):
    return create_mcp_server(registry=registry, name="Test Castor MCP")


# ── Test: Tool listing ──────────────────────────────────────────────────────


async def test_lists_castor_tools_and_meta_tools(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}

        # Castor tools
        assert "search" in tool_names
        assert "delete_item" in tool_names
        assert "send_email" in tool_names
        assert "free_tool" in tool_names

        # Meta-tools
        assert "castor_init" in tool_names
        assert "castor_status" in tool_names
        assert "castor_approve" in tool_names
        assert "castor_reject" in tool_names
        assert "castor_modify" in tool_names


async def test_tool_descriptions_include_castor_hints(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        tool_map = {t.name: t for t in tools}

        assert "DESTRUCTIVE" in (tool_map["delete_item"].description or "")
        assert "Cost: 1.0 api" in (tool_map["search"].description or "")


# ── Test: Session initialization ────────────────────────────────────────────


async def test_tool_requires_init(server):
    async with Client(server) as client:
        result = await client.call_tool("search", {"query": "hello"})
        text = result.content[0].text
        assert "castor_init" in text


async def test_init_session(server):
    async with Client(server) as client:
        result = await client.call_tool("castor_init", {"budgets": {"api": 10.0}})
        text = result.content[0].text
        assert "initialized" in text.lower()
        assert "api: 10.0" in text


async def test_double_init_rejected(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})
        result = await client.call_tool("castor_init", {"budgets": {"api": 20.0}})
        text = result.content[0].text
        assert "already initialized" in text.lower()


# ── Test: Budget enforcement ────────────────────────────────────────────────


async def test_budget_deduction(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 5.0}})

        result = await client.call_tool("search", {"query": "hello"})
        text = result.content[0].text
        assert "Result for 'hello'" in text

        # Check status: 5.0 - 1.0 = 4.0 remaining
        status = await client.call_tool("castor_status", {})
        assert "4.00" in status.content[0].text
        assert "5.00" in status.content[0].text


async def test_budget_exhaustion(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 0.5}})

        result = await client.call_tool("search", {"query": "hello"})
        text = result.content[0].text
        assert "Budget exhausted" in text


async def test_free_tool_no_budget_deduction(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 5.0}})

        result = await client.call_tool("free_tool", {})
        assert "free result" in result.content[0].text

        # Budget unchanged
        status = await client.call_tool("castor_status", {})
        assert "5.00 / 5.00" in status.content[0].text


# ── Test: HITL flow ─────────────────────────────────────────────────────────


async def test_destructive_tool_returns_pending(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        result = await client.call_tool("delete_item", {"item_id": "abc"})
        data = json.loads(result.content[0].text)
        assert data["status"] == "pending_approval"
        assert data["tool_name"] == "delete_item"
        assert "request_id" in data


async def test_hitl_approve_executes_tool(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        # Call destructive tool → pending
        result = await client.call_tool("delete_item", {"item_id": "abc"})
        data = json.loads(result.content[0].text)
        request_id = data["request_id"]

        # Approve → executes
        result = await client.call_tool("castor_approve", {"request_id": request_id})
        data = json.loads(result.content[0].text)
        assert data["status"] == "approved"
        assert "Deleted abc" in str(data["result"])


async def test_hitl_reject_refunds_budget(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        result = await client.call_tool("delete_item", {"item_id": "abc"})
        data = json.loads(result.content[0].text)
        request_id = data["request_id"]

        # Reject → refund
        result = await client.call_tool(
            "castor_reject", {"request_id": request_id, "reason": "Not allowed"}
        )
        data = json.loads(result.content[0].text)
        assert data["status"] == "rejected"
        assert data["reason"] == "Not allowed"

        # Budget should be fully refunded
        status = await client.call_tool("castor_status", {})
        assert "10.00 / 10.00" in status.content[0].text


async def test_hitl_modify_refunds_and_returns_feedback(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        result = await client.call_tool("delete_item", {"item_id": "abc"})
        data = json.loads(result.content[0].text)
        request_id = data["request_id"]

        # Modify → refund + feedback
        result = await client.call_tool(
            "castor_modify",
            {
                "request_id": request_id,
                "feedback": "Only delete files older than 30 days",
            },
        )
        data = json.loads(result.content[0].text)
        assert data["status"] == "modified"
        assert "older than 30 days" in data["feedback"]

        # Budget refunded
        status = await client.call_tool("castor_status", {})
        assert "10.00 / 10.00" in status.content[0].text


async def test_requires_hitl_tool_also_suspends(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        result = await client.call_tool(
            "send_email", {"to": "user@example.com", "body": "Hello"}
        )
        data = json.loads(result.content[0].text)
        assert data["status"] == "pending_approval"
        assert data["tool_name"] == "send_email"


async def test_approve_nonexistent_request(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        result = await client.call_tool("castor_approve", {"request_id": "nonexistent"})
        assert "No pending request" in result.content[0].text


# ── Test: Status ────────────────────────────────────────────────────────────


async def test_status_shows_pending_hitl(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})
        await client.call_tool("delete_item", {"item_id": "xyz"})

        status = await client.call_tool("castor_status", {})
        text = status.content[0].text
        assert "Pending approvals (1)" in text
        assert "delete_item" in text


async def test_status_shows_tool_call_count(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})
        await client.call_tool("search", {"query": "a"})
        await client.call_tool("search", {"query": "b"})

        status = await client.call_tool("castor_status", {})
        assert "Total tool calls: 2" in status.content[0].text


# ── Test: Async tools ───────────────────────────────────────────────────────


async def test_async_tool_with_approval(server):
    async with Client(server) as client:
        await client.call_tool("castor_init", {"budgets": {"api": 10.0}})

        # send_email requires HITL
        result = await client.call_tool(
            "send_email", {"to": "test@test.com", "body": "Hi"}
        )
        data = json.loads(result.content[0].text)
        request_id = data["request_id"]

        # Approve
        result = await client.call_tool("castor_approve", {"request_id": request_id})
        data = json.loads(result.content[0].text)
        assert data["status"] == "approved"
        assert "Sent to test@test.com" in str(data["result"])


# ── Test: Server factory ────────────────────────────────────────────────────


def test_create_server_with_tools():
    """Test that create_mcp_server works with explicit tool list."""
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    def my_tool(x: int) -> int:
        return x * 2

    server = create_mcp_server(tools=[my_tool])
    assert server is not None


def test_create_server_rejects_non_castor_tool():
    """Test that non-@castor_tool functions are rejected."""

    def plain_function(x: int) -> int:
        return x

    with pytest.raises(TypeError, match="not a @castor_tool"):
        create_mcp_server(tools=[plain_function])
