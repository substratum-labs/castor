"""Castor MCP Server: expose @castor_tool functions as MCP tools."""

from castor.mcp.server import CastorMCPTool, create_mcp_server

__all__ = ["CastorMCPTool", "create_mcp_server"]
