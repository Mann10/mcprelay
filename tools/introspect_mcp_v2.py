"""Second-pass introspection: exact signatures needed to write the gateway."""
import importlib
import inspect


def sig(label, obj, note=""):
    print(f"\n--- {label} ---")
    if obj is None:
        print("  MISSING")
        return
    try:
        print(f"  {inspect.signature(obj)}")
    except (ValueError, TypeError):
        print("  <no signature>")
    doc = (inspect.getdoc(obj) or "").strip().splitlines()
    for line in doc[:14]:
        print(f"  | {line}")
    if note:
        print(f"  ({note})")


print("=" * 78)
print("CLIENT TRANSPORTS")
print("=" * 78)

sh = importlib.import_module("mcp.client.streamable_http")
sig("streamable_http_client", getattr(sh, "streamable_http_client", None))
print("\n  module constants:")
for c in ("MODERN_PROTOCOL_VERSIONS", "MCP_PROTOCOL_VERSION_HEADER", "MCP_SESSION_ID",
          "MCP_METHOD_HEADER", "MCP_NAME_HEADER", "HEADER_MISMATCH", "UNSUPPORTED_PROTOCOL_VERSION"):
    print(f"    {c} = {getattr(sh, c, '<absent>')!r}")

t = importlib.import_module("mcp.types")
print("\n  version constants:")
for c in ("LATEST_PROTOCOL_VERSION", "DEFAULT_NEGOTIATED_VERSION", "UNSUPPORTED_PROTOCOL_VERSION",
          "PROTOCOL_VERSION_META_KEY", "CLIENT_INFO_META_KEY", "CLIENT_CAPABILITIES_META_KEY",
          "SERVER_INFO_META_KEY"):
    print(f"    {c} = {getattr(t, c, '<absent>')!r}")

print("\n" + "=" * 78)
print("CLIENT SESSION")
print("=" * 78)
CS = importlib.import_module("mcp.client.session")
for name in ("ClientSession",):
    cls = getattr(CS, name, None)
    print(f"\n{mcs if False else ''}{name} members:")
    for m in sorted(n for n in dir(cls) if not n.startswith("_")):
        obj = getattr(cls, m, None)
        if callable(obj):
            try:
                s = str(inspect.signature(obj))
            except (ValueError, TypeError):
                s = ""
            print(f"    {m}{s}")
        else:
            print(f"    {m}  (attribute)")

print("\n" + "=" * 78)
print("SERVER SIDE")
print("=" * 78)
ll = importlib.import_module("mcp.server.lowlevel")
Server = ll.Server
for m in ("run", "streamable_http_app", "create_initialization_options", "get_capabilities"):
    sig(f"Server.{m}", getattr(Server, m, None))

shs = importlib.import_module("mcp.server.streamable_http")
print("\n  mcp.server.streamable_http members:")
print("    " + ", ".join(n for n in dir(shs) if not n.startswith("_")))

print("\n  streamable http server classes:")
for n in dir(shs):
    if n.startswith("_"):
        continue
    o = getattr(shs, n)
    if inspect.isclass(o):
        try:
            print(f"    {n}{inspect.signature(o)}")
        except (ValueError, TypeError):
            print(f"    {n}  (no signature)")

print("\n  stateless-related names anywhere in mcp.server:")
import subprocess
import mcp
root = mcp.__file__.rsplit("\\", 1)[0].rsplit("/", 1)[0]
for kw in ("stateless", "Stateless", "json_response", "JSONResponseMode"):
    hits = []
    for dirpath, _dirs, files in __import__("os").walk(root):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = __import__("os").path.join(dirpath, f)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except OSError:
                continue
            if kw in txt:
                hits.append(p.replace(root, "mcp"))
    print(f"    {kw}: {len(hits)} files")
    for h in hits[:6]:
        print(f"        {h}")

print("\n" + "=" * 78)
print("TYPES OF INTEREST")
print("=" * 78)
for n in ("Tool", "ListToolsResult", "CallToolRequestParams", "CallToolResult",
          "DiscoverResult", "PaginatedRequestParams", "Implementation",
          "InputRequiredResult", "InputResponses", "CacheableResult"):
    c = getattr(t, n, None)
    if c is None:
        print(f"\n{n}: MISSING")
        continue
    fields = getattr(c, "model_fields", {})
    print(f"\n{n}: " + (", ".join(f"{k}" for k in fields) or "<no fields>"))

print("\n" + "=" * 78)
print("HIGH-LEVEL Client / ClientSessionGroup")
print("=" * 78)
c_mod = importlib.import_module("mcp.client.client")
sig("Client.__init__", getattr(c_mod, "Client", None))
for m in ("connect", "list_tools", "call_tool"):
    sig(f"Client.{m}", getattr(getattr(c_mod, "Client", object), m, None))

sg = importlib.import_module("mcp.client.session_group")
for m in ("ClientSessionGroup",):
    cls = getattr(sg, m, None)
    print(f"\n{m} members: " + ", ".join(n for n in dir(cls) if not n.startswith("_")))
    for mm in ("connect_to_server", "__aenter__", "call_tool", "list_tools"):
        o = getattr(cls, mm, None)
        if o:
            try:
                print(f"    {mm}{inspect.signature(o)}")
            except (ValueError, TypeError):
                pass
