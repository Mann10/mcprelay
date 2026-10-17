"""Introspect the installed MCP SDK so gateway code targets the real v2 API."""
import importlib
import inspect
import pkgutil
import sys

import mcp

print("=" * 78)
print(f"mcp version : {getattr(mcp, '__version__', 'n/a')}")
print(f"location    : {mcp.__file__}")
print("=" * 78)

print("\n### top-level exports")
print(sorted(n for n in dir(mcp) if not n.startswith("_")))

print("\n### submodules")
import mcp as pkg
for mod in pkgutil.iter_modules(pkg.__path__):
    print("  mcp." + mod.name + (f"  (package)" if mod.ispkg else ""))

CANDIDATES = [
    "mcp.server",
    "mcp.server.fastmcp",
    "mcp.server.lowlevel",
    "mcp.client",
    "mcp.client.stdio",
    "mcp.client.streamable_http",
    "mcp.client.sse",
    "mcp.types",
]
print("\n### candidate module members")
for name in CANDIDATES:
    try:
        m = importlib.import_module(name)
    except Exception as e:
        print(f"\n{name}: FAILED -> {e}")
        continue
    members = [n for n in dir(m) if not n.startswith("_")]
    print(f"\n{name}:")
    print("  " + ", ".join(members) if members else "  (empty)")

print("\n### key signatures")
for dotted, attrs in [
    ("mcp.server.lowlevel", ["Server", "ServerSession"]),
    ("mcp.client.stdio", ["stdio_client", "StdioServerParameters"]),
    ("mcp.client.streamable_http", ["streamablehttp_client"]),
    ("mcp.client.sse", ["sse_client"]),
    ("mcp", ["ClientSession"]),
]:
    try:
        m = importlib.import_module(dotted)
    except Exception:
        continue
    for a in attrs:
        obj = getattr(m, a, None)
        if obj is None:
            print(f"\n{dotted}.{a}: MISSING")
            continue
        try:
            sig = str(inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "<no signature>"
        print(f"\n{dotted}.{a}{sig}")
        meths = [n for n in dir(obj) if not n.startswith("_")]
        print("   members: " + ", ".join(meths[:60]))

print("\n### grep for 2026-07-28 / server_discover in the package")
import subprocess
root = mcp.__file__.rsplit("\\", 1)[0].rsplit("/", 1)[0]
try:
    out = subprocess.run(
        ["grep", "-rn", "-l", "-e", "2026-07-28", "-e", "server/discover", "-e", "server_discover", root],
        capture_output=True, text=True, timeout=60,
    )
    print(out.stdout[:4000] or "(no matches)")
except Exception as e:
    print("grep failed:", e)
