"""Minimal stdio MCP server for fast, dependency-free live tests (replaces
slow `npx` in e2e probes). Exposes two trivial tools."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("dummy")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


@server.tool()
def shout(text: str) -> str:
    """Uppercase the text."""
    return text.upper()


if __name__ == "__main__":
    server.run(transport="stdio")
