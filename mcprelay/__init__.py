"""mcprelay - a local, config-driven MCP gateway.

Aggregates several upstream MCP servers behind one endpoint, namespaces their
tools, enforces a per-gateway allow-list, and speaks the modern stateless
MCP `2026-07-28` protocol (dual-era on the wire, so legacy clients still work).

Upstreams may be stdio subprocesses, Streamable HTTP, or legacy SSE.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
