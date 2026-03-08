"""Example Castor tools for the MCP server demo."""

from castor.gate.decorator import castor_tool


@castor_tool(consumes="api", cost_per_use=0.5)
def web_search(query: str) -> list[str]:
    """Search the web for documents matching the query."""
    return [f"Result 1 for '{query}'", f"Result 2 for '{query}'"]


@castor_tool(consumes="disk", cost_per_use=1.0)
async def write_file(filename: str, content: str) -> str:
    """Write content to a file."""
    return f"Wrote {len(content)} chars to {filename}"


@castor_tool(consumes="disk", cost_per_use=1.0, destructive=True)
def delete_file(filename: str) -> str:
    """Delete a file from the filesystem. Destructive — requires approval."""
    return f"Deleted {filename}"


@castor_tool(consumes="api", cost_per_use=0.0)
def check_balance() -> dict[str, float]:
    """Check the current account balance. Free operation."""
    return {"balance": 1250.00, "currency": "USD"}
