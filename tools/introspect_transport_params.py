"""Third-pass: how to attach headers/auth to remote upstreams, and how a client
can be constructed for a URL with custom headers."""
import importlib
import inspect

def dump(label, cls):
    print(f"\n--- {label} ---")
    if cls is None:
        print("  MISSING")
        return
    try:
        print(f"  sig: {inspect.signature(cls)}")
    except (ValueError, TypeError):
        print("  sig: <none>")
    fields = getattr(cls, "model_fields", None)
    if fields:
        for k, v in fields.items():
            print(f"    field {k}: {v.annotation}  default={v.default!r}")
    for line in (inspect.getdoc(cls) or "").splitlines()[:16]:
        print(f"  | {line}")

sg = importlib.import_module("mcp.client.session_group")
for n in ("StreamableHttpParameters", "SseServerParameters", "ClientSessionParameters"):
    dump(f"session_group.{n}", getattr(sg, n, None))

grp = getattr(sg, "ClientSessionGroup", None)
for m in ("tools", "call_tool", "connect_with_session", "disconnect_from_server", "sessions"):
    o = getattr(grp, m, None)
    if o is None:
        print(f"\nClientSessionGroup.{m}: MISSING")
        continue
    print(f"\nClientSessionGroup.{m}")
    try:
        print(f"  sig: {inspect.signature(o)}")
    except (ValueError, TypeError):
        pass
    for line in (inspect.getdoc(o) or "").splitlines()[:22]:
        print(f"  | {line}")

tr = importlib.import_module("mcp.client.session")
print("\n--- mcp.client.session.Transport ---")
T = getattr(tr, "Transport", None)
print("  members:", ", ".join(n for n in dir(T) if not n.startswith("_")) if T else "MISSING")

# How does Client turn a URL string into a transport?
import mcp.client.client as cc
src = inspect.getsource(cc)
print("\n" + "=" * 78)
print("Client URL -> transport construction (source excerpt)")
print("=" * 78)
lines = src.splitlines()
for i, ln in enumerate(lines):
    if any(k in ln for k in ("StreamableHttpParameters", "SseServerParameters",
                             "http_client", "httpx", "isinstance(self._server, str)",
                             "def _create", "headers")):
        lo, hi = max(0, i - 4), min(len(lines), i + 12)
        print(f"\n  [lines {lo}-{hi}]")
        for j in range(lo, hi):
            print(f"  {j:4d}| {lines[j]}")
        print("  ...")
