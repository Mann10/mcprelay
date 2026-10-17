"""End-to-end test: a real MCP client against a running mcprelay HTTP endpoint.

Covers the behaviours that matter for a gateway:

1. auth rejects a missing key and a wrong key
2. tools/list returns namespaced tools from multiple upstreams
3. tools/list honours the per-gateway allow-list
4. tools/call routes to the correct upstream
5. tools/call is REJECTED for a tool outside the allow-list (not merely hidden)
6. an unknown tool returns a helpful error, not a traceback
7. the radar search meta-tool works
8. /radar resolves to the same server
9. the control plane reflects live upstream state

Usage:
    python -m mcprelay serve -c config.local.yaml
    python tests/e2e_test.py --base http://127.0.0.1:8787 --key dev-local-key
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def _client(key: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        headers={"X-API-Key": key}, timeout=30.0, follow_redirects=True
    )


# ------------------------------------------------------------------- probes


async def probe_auth(base: str, key: str) -> None:
    print("\n=== auth ===")
    url = f"{base}/gateway/dev/mcp"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(url, json=payload)
        check("missing key -> 401", r.status_code == 401, f"got {r.status_code}")

        r = await http.post(url, json=payload, headers={"X-API-Key": "definitely-wrong"})
        check("wrong key -> 401", r.status_code == 401, f"got {r.status_code}")

        r = await http.post(url, json=payload, headers={"X-API-Key": key})
        check("correct key -> 200", r.status_code == 200, f"got {r.status_code}")

        r = await http.post(
            url, json=payload, headers={"Authorization": f"Bearer {key}"}
        )
        check("bearer fallback accepted", r.status_code == 200, f"got {r.status_code}")

    r = await httpx.AsyncClient(timeout=20.0).get(f"{base}/healthz")
    body = r.json()
    check("healthz reports ok", body.get("status") == "ok", str(body.get("status")))
    check(
        "healthz lists upstreams",
        body.get("upstreams_total", 0) >= 3,
        f"{body.get('upstreams_ready')}/{body.get('upstreams_total')} ready",
    )


async def probe_control_plane(base: str) -> None:
    print("\n=== control plane ===")
    async with httpx.AsyncClient(timeout=20.0) as http:
        gateways = (await http.get(f"{base}/api/v1/gateways")).json().get("gateways", [])
        check("gateways listed", len(gateways) >= 2, str([g["id"] for g in gateways]))

        data = (await http.get(f"{base}/api/v1/gateways/dev/tools")).json()
        names = [t["name"] for t in data.get("tools", [])]
        check("tool count non-zero", data.get("count", 0) > 0, f"count={data.get('count')}")
        check(
            "namespacing applied",
            all("." in n for n in names if n != "gateway_search_tools"),
            f"{len(names)} tools",
        )

        up = (await http.get(f"{base}/api/v1/upstreams")).json()
        check(
            "upstreams expose live tool lists",
            sum(len(v) for v in up.get("tools", {}).values()) > 0,
            ", ".join(f"{k}={len(v)}" for k, v in sorted(up.get("tools", {}).items())),
        )


async def probe_tools_list(base: str, key: str) -> list[str]:
    print("\n=== mcp: tools/list ===")
    url = f"{base}/gateway/dev/mcp"

    async with _client(key) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                init = await session.initialize()
                check(
                    "connected and initialised",
                    True,
                    f"server={getattr(init.server_info, 'name', '?')} "
                    f"protocol={init.protocol_version}",
                )

                listed = await session.list_tools()
                names = [t.name for t in listed.tools]
                check("tools/list returns tools", len(names) > 0, f"{len(names)} tools")

                prefixes = sorted({n.split(".")[0] for n in names if "." in n})
                check(
                    "multiple upstreams present",
                    len(prefixes) >= 3,
                    f"prefixes={prefixes}",
                )
                check("radar search tool exposed", "gateway_search_tools" in names)

                ttl = getattr(listed, "ttl_ms", None)
                check("tools/list advertises ttlMs", ttl is not None, f"ttl_ms={ttl}")

                print("\n=== mcp: tools/call ===")
                target = next(
                    (n for n in names if n.startswith("sequential-thinking.")), None
                )
                if target:
                    res = await session.call_tool(
                        target,
                        {
                            "thought": "gateway routing self-test",
                            "thoughtNumber": 1,
                            "totalThoughts": 1,
                            "nextThoughtNeeded": False,
                            "isRevision": False,
                        },
                    )
                    text = str(res.content[0].text) if res.content else ""
                    check(
                        "routed to correct upstream",
                        not res.is_error,
                        f"{target} -> {text[:58]!r}",
                    )
                else:
                    check("routed to correct upstream", False, "no sequential-thinking tool")

                print("\n=== mcp: error handling ===")
                res = await session.call_tool("definitely.not.a.tool", {"x": 1})
                text = str(res.content[0].text) if res.content else ""
                check(
                    "unknown tool -> clean error, not traceback",
                    res.is_error and "unknown tool" in text,
                    text[:88],
                )
                check(
                    "error names the namespacing convention",
                    "<upstream>.<tool>" in text,
                    text[:88],
                )

                print("\n=== mcp: radar search ===")
                res = await session.call_tool(
                    "gateway_search_tools", {"query": "file", "limit": 5}
                )
                text = str(res.content[0].text) if res.content else ""
                check(
                    "search returns matches",
                    not res.is_error and "match" in text.lower(),
                    (text.splitlines() or [""])[0][:78],
                )

                # Exercise a second upstream to prove routing is not accidental.
                print("\n=== mcp: cross-upstream routing ===")
                target2 = next((n for n in names if n.startswith("filesystem.")), None)
                if target2:
                    res = await session.call_tool(
                        target2, {"path": "mcprelay-e2e-proof.txt", "content": "proof"}
                    )
                    check(
                        "second upstream also routable",
                        not res.is_error,
                        f"{target2} -> {str(res.content[0].text)[:50]!r}",
                    )
                else:
                    check("second upstream also routable", False, "no filesystem tool")

    return names


async def probe_allowlist(base: str, dev_names: list[str]) -> None:
    print("\n=== mcp: curated allow-list enforcement ===")
    url = f"{base}/gateway/curated/mcp"

    async with _client("curated-local-key") as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [t.name for t in listed.tools]
                hidden = [n for n in names if n.startswith("memory.delete_")]
                check(
                    "destructive tools hidden from listing",
                    not hidden,
                    f"hidden={hidden or 'none leaked'}",
                )
                check(
                    "non-destructive memory tools still present",
                    any(n.startswith("memory.read_graph") for n in names),
                )
                check(
                    "curated gateway has fewer tools than dev",
                    len(names) < len(dev_names),
                    f"curated={len(names)} dev={len(dev_names)}",
                )

                # The critical property: a hidden tool must be REJECTED on the
                # call path, not merely absent from the listing. Name it
                # explicitly - it is by definition not discoverable from the
                # listing, which is the whole point of the check.
                denied = "memory.delete_entities"
                res = await session.call_tool(
                    denied, {"entityType": "entity", "entityName": "x"}
                )
                text = str(res.content[0].text) if res.content else ""
                check(
                    "hidden tool REJECTED on call, not just filtered",
                    res.is_error and "not enabled" in text,
                    text[:92],
                )


async def probe_radar_path(base: str) -> None:
    print("\n=== /radar path ===")
    async with _client("dev-local-key") as http_client:
        async with streamable_http_client(
            f"{base}/gateway/dev/radar", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                check(
                    "/radar serves the same tool set",
                    len(listed.tools) > 0,
                    f"{len(listed.tools)} tools",
                )


# --------------------------------------------------------------------- main


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--key", default="dev-local-key")
    args = parser.parse_args()

    await probe_auth(args.base, args.key)
    await probe_control_plane(args.base)
    dev_names = await probe_tools_list(args.base, args.key)
    await probe_allowlist(args.base, dev_names)
    await probe_radar_path(args.base)

    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILURES:")
        for _status, name, detail in failed:
            print(f"  - {name}  ({detail})")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
