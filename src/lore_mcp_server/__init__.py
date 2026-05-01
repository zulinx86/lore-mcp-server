"""MCP server for searching and reading lore.kernel.org mailing list archives."""

from lore_mcp_server.server import mcp


def main():
    """Entry point for the MCP server."""
    mcp.run()
