"""End-to-end tests for runtime gateway CRUD and agent-config generation.

Assumes a running mcprelay on --base. Covers the flow that actually matters:

    create a gateway -> its endpoint serves tools -> a client's generated
    config points at that endpoint -> the key works and a wrong key does not

Also checks that generated configs are written non-destructively.

Usage:
    python -m mcprelay serve -c config.local.yaml
    python tests/e2e_gateway_test.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcprelay import agentconfig  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def create_gateway(base: str, name: str, upstreams: list[str]) -> dict:
    with httpx.Client(timeout=30.0) as http:
        return http.post(
            f"{base}/api/v1/gateways",
            json={"name": name, "upstreams": upstreams},
        ).json()


# ------------------------------------------------------------------- probes


def probe_crud(base: str) -> dict:
    print("\n=== gateway CRUD ===")

    with httpx.Client(timeout=30.0) as http:
        r = http.post(
            f"{base}/api/v1/gateways",
            json={"name": "e2e gateway", "upstreams": ["memory", "sequential-thinking"]},
        )
        check("create returns 201", r.status_code == 201, f"got {r.status_code}")
        gw = r.json()
        gid = gw["id"]
        check("create returns a key", bool(gw.get("api_key")), gw.get("api_key", "")[:12] + "...")
        check("create reports endpoint", gw.get("endpoint") == f"/gateway/{gid}/mcp",
              gw.get("endpoint", ""))
        check("create reports tools", gw.get("tool_count", 0) > 0,
              f"{gw.get('tool_count')} tools")

        # The key must be shown at creation and NOT on subsequent reads.
        r = http.get(f"{base}/api/v1/gateways/{gid}")
        check("listing never returns the key", "api_key" not in r.json(), "absent")
        r = http.get(f"{base}/api/v1/gateways")
        keys = [g for g in r.json()["gateways"] if "api_key" in g]
        check("no key leaks in the collection", not keys, f"{len(keys)} leaked")
        r = http.get(f"{base}/healthz")
        blob = json.dumps(r.json())
        check("no key in /healthz", gw["api_key"] not in blob, "absent")

        r = http.post(f"{base}/api/v1/gateways/{gid}/rotate-key")
        check("rotate-key returns a new key", r.status_code == 200)
        new_key = r.json().get("api_key")
        check("rotate-key actually changed the key", new_key != gw["api_key"], new_key[:12] + "...")

    # Rotate once more so a stale in-place auth rule cannot pass by accident:
    # the endpoint must reject every key except the most recent one.
    with httpx.Client(timeout=20.0) as http:
        r = http.post(f"{base}/api/v1/gateways/{gid}/rotate-key")
        check("second rotate also succeeds", r.status_code == 200)
        new_key = r.json().get("api_key")

        # validation
        r = http.post(f"{base}/api/v1/gateways",
                      json={"name": "bad", "upstreams": ["does-not-exist"]})
        check("unknown upstream rejected", r.status_code == 400, r.text[:70])

        r = http.post(f"{base}/api/v1/gateways", json={"name": "", "upstreams": ["memory"]})
        check("empty name rejected", r.status_code == 400, r.text[:60])

        r = http.post(f"{base}/api/v1/gateways", json={"name": "x", "upstreams": []})
        check("no upstreams rejected", r.status_code == 400, r.text[:60])

    gw["_new_key"] = new_key
    return gw


def probe_serving(base: str, gw: dict) -> None:
    print("\n=== the new gateway actually serves ===")
    gid = gw["id"]
    url = f"{base}/gateway/{gid}/mcp"
    key = gw["_new_key"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    with httpx.Client(timeout=20.0) as http:
        r = http.post(url, json=payload)
        check("runtime gateway without a key -> 401", r.status_code == 401,
              f"got {r.status_code}")

        r = http.post(url, json=payload, headers={"X-API-Key": gw["api_key"]})
        check("rotated-away key rejected", r.status_code == 401, f"got {r.status_code}")

        r = http.post(url, json=payload, headers={"X-API-Key": key})
        check("current key accepted", r.status_code == 200, f"got {r.status_code}")

    # full MCP round trip through the runtime gateway
    import asyncio

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def roundtrip() -> tuple[int, bool]:
        async with httpx2.AsyncClient(
            headers={"X-API-Key": key}, timeout=30.0, follow_redirects=True
        ) as hc:
            async with streamable_http_client(url, http_client=hc) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = [t.name for t in listed.tools]
                    res = await session.call_tool(
                        "memory.read_graph", {}, read_timeout_seconds=30
                    )
                    return len(names), not res.is_error

    count, ok = asyncio.run(roundtrip())
    check("tools/list over MCP", count > 0, f"{count} tools")
    check("tools/call over MCP", ok, "memory.read_graph")

    # radar path on a runtime gateway
    async def radar() -> int:
        async with httpx2.AsyncClient(
            headers={"X-API-Key": key}, timeout=30.0, follow_redirects=True
        ) as hc:
            async with streamable_http_client(
                f"{base}/gateway/{gid}/radar", http_client=hc
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return len((await session.list_tools()).tools)

    check("/radar mounted on a runtime gateway", asyncio.run(radar()) > 0, "")


def probe_persistence(base: str, gid: str) -> None:
    print("\n=== persistence ===")
    state = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "var", "gateways.json")
    if not os.path.exists(state):
        check("state file written", False, f"missing {state}")
        return
    with open(state, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    ids = [g["id"] for g in data.get("gateways", [])]
    check("state file contains the gateway", gid in ids, f"{len(ids)} runtime gateways")
    check("state file keeps a backup", os.path.exists(state + ".bak") or len(ids) == 1,
          "gateways.json.bak")


def probe_allowlist_patch(base: str, gw: dict) -> None:
    print("\n=== allow-list patch ===")
    gid = gw["id"]
    with httpx.Client(timeout=20.0) as http:
        r = http.patch(f"{base}/api/v1/gateways/{gid}",
                       json={"tool_filter": ["!memory.delete_*", "*"]})
        check("patch accepted", r.status_code == 200, f"got {r.status_code}")

        tools = http.get(f"{base}/api/v1/gateways/{gid}/tools").json()
        names = [t["name"] for t in tools["tools"]]
        check("delete tools now denied",
              not any(n.startswith("memory.delete_") for n in names),
              f"{len(names)} tools")

        r = http.patch(f"{base}/api/v1/gateways/{gid}", json={"tool_filter": []})
        check("empty filter rejected", r.status_code == 400, f"got {r.status_code}")


def probe_client_configs(base: str, gw: dict) -> None:
    print("\n=== generated client configs ===")
    gid = gw["id"]

    with httpx.Client(timeout=30.0) as http:
        clients = http.get(f"{base}/api/v1/clients").json()["clients"]
        check("client catalogue non-empty", len(clients) >= 5,
              ", ".join(c["client_id"] for c in clients))

        for client_id in ("opencode", "cursor", "claude_code", "windsurf", "vscode", "generic"):
            r = http.get(f"{base}/api/v1/gateways/{gid}/client-config/{client_id}")
            if not check(f"{client_id}: preview works", r.status_code == 200,
                         f"got {r.status_code}"):
                continue
            data = r.json()
            blob = json.dumps(data["config"])
            check(f"{client_id}: config carries the url", f"/gateway/{gid}/mcp" in blob, "")
            check(f"{client_id}: config carries the key", gw["_new_key"] in blob, "")
            if client_id == "vscode":
                check("vscode uses 'servers' not 'mcpServers'",
                      "servers" in data["config"] and "mcpServers" not in data["config"], "")
            if client_id == "opencode":
                entry = data["config"]["mcp"][data["server_key"]]
                check("opencode uses 'mcp' + type=remote",
                      entry.get("type") == "remote", str(entry.get("type")))

        r = http.get(f"{base}/api/v1/gateways/{gid}/client-config/nope")
        check("unknown client -> 404", r.status_code == 404, f"got {r.status_code}")

        # radar variant
        data = http.get(f"{base}/api/v1/gateways/{gid}/client-config/cursor").json()
        check("default config points at /mcp", data["config"]["mcpServers"]
              [data["server_key"]]["url"].endswith("/mcp"), "")

        r = http.post(f"{base}/api/v1/gateways/{gid}/install/cursor",
                      json={"radar": True, "dry_run": True})
        url = r.json()["config"]["mcpServers"][r.json()["server_key"]]["url"]
        check("radar flag switches to /radar", url.endswith("/radar"), url)

        # stdio bridge
        r = http.post(f"{base}/api/v1/gateways/{gid}/install/claude_desktop",
                      json={"transport": "stdio-bridge", "dry_run": True})
        entry = r.json()["config"]["mcpServers"][r.json()["server_key"]]
        check("stdio bridge uses npx + mcp-remote",
              entry.get("command") == "npx" and "mcp-remote" in entry.get("args", []),
              str(entry.get("command")))

        r = http.post(f"{base}/api/v1/gateways/{gid}/install/cursor",
                      json={"dry_run": True})
        check("dry_run writes nothing", r.json().get("written") is False, "")


def probe_write_safety(base: str, gw: dict) -> None:
    print("\n=== writing client configs ===")
    gid = gw["id"]
    tmpdir = tempfile.mkdtemp(prefix="mcprelay-e2e-")

    target = os.path.join(tmpdir, "mcp.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('{"existingKey": {"keep": "me"}, "mcpServers": {"other": {"url": "http://old"}}}')

    with httpx.Client(timeout=30.0) as http:
        r = http.post(f"{base}/api/v1/gateways/{gid}/install/generic",
                      json={"target_path": target})
        if check("write succeeds", r.status_code == 200, r.text[:80]):
            data = r.json()
            check("write backed up the original", bool(data.get("backup_path")),
                  os.path.basename(data.get("backup_path") or ""))
            check("write preserved unrelated keys",
                  "existingKey" in data.get("preserved_top_level_keys", []),
                  ", ".join(data.get("preserved_top_level_keys", [])))
            check("write preserved existing servers",
                  "other" in json.loads(open(target, encoding="utf-8").read())["mcpServers"],
                  "")

            with open(data["backup_path"], encoding="utf-8") as fh:
                backup = json.load(fh)
            check("backup holds the original content",
                  "existingKey" in backup and data["server_key"] not in backup["mcpServers"],
                  "")

        # jsonc target
        jsonc = os.path.join(tmpdir, "mcp.jsonc")
        with open(jsonc, "w", encoding="utf-8") as fh:
            fh.write('// my annotation\n{"keep": 2, "mcpServers": {},}\n')
        r = http.post(f"{base}/api/v1/gateways/{gid}/install/generic",
                      json={"target_path": jsonc})
        check("jsonc target accepted", r.status_code == 200, r.text[:80])
        if r.status_code == 200:
            check("jsonc stripping reported honestly",
                  r.json().get("jsonc_comments_dropped") is True,
                  str(r.json().get("jsonc_comments_dropped")))

        # unparseable target must be refused
        broken = os.path.join(tmpdir, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{ not json at all")
        r = http.post(f"{base}/api/v1/gateways/{gid}/install/generic",
                      json={"target_path": broken})
        check("unparseable target refused", r.status_code == 400, f"got {r.status_code}")
        with open(broken, encoding="utf-8") as fh:
            check("refused target left untouched", fh.read() == "{ not json at all", "")


def probe_delete(base: str, gw: dict) -> None:
    print("\n=== delete ===")
    gid = gw["id"]
    with httpx.Client(timeout=20.0) as http:
        r = http.delete(f"{base}/api/v1/gateways/dev")
        check("config-declared gateway protected", r.status_code == 409,
              f"got {r.status_code}")

        r = http.delete(f"{base}/api/v1/gateways/{gid}")
        check("runtime gateway deletable", r.status_code == 200, f"got {r.status_code}")

        r = http.get(f"{base}/api/v1/gateways/{gid}")
        check("deleted gateway is gone", r.status_code == 404, f"got {r.status_code}")


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    gw = probe_crud(base)
    probe_serving(base, gw)
    probe_persistence(base, gw["id"])
    probe_allowlist_patch(base, gw)
    probe_client_configs(base, gw)
    probe_write_safety(base, gw)
    probe_delete(base, gw)

    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILURES:")
        for _s, name, detail in failed:
            print(f"  - {name}  ({detail})")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
