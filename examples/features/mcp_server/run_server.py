"""Start the Castor MCP server with demo tools.

Usage:
    uv run python examples/mcp_server/run_server.py
    uv run python examples/mcp_server/run_server.py --transport sse
"""

from examples.mcp_server.demo_tools import (
    check_balance,
    delete_file,
    web_search,
    write_file,
)

from castor.mcp.server import create_mcp_server

server = create_mcp_server(
    tools=[web_search, write_file, delete_file, check_balance],
    name="Castor Demo MCP Server",
)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server.run(transport=args.transport)
