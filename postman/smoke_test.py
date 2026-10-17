"""Smoke-test every request in the Postman collection against a live mcprelay.

Resolves each URL template with the collection's declared variable defaults,
issues the request, and reports pass/fail. This catches a collection that is
valid JSON but points at endpoints that no longer exist.

Read-only by default: create/patch/rotate/delete/install-write are skipped
unless --destructive is passed.

Usage:
    python smoke_test.py [--base http://127.0.0.1:8787] [--destructive]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, "mcprelay.postman_collection.json")

MUTATING = {
    "Create gateway",
    "Patch allow-list",
    "Rotate key",
    "Delete gateway",
    "Install client config (WRITE)",
    "Install to Claude Desktop (stdio bridge)",
    "Reconnect an upstream",
}

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)


def resolve(url: str, variables: dict[str, str]) -> str:
    return re.sub(
        r"\{\{([a-z_][a-z0-9_]*)\}\}",
        lambda m: variables.get(m.group(1), m.group(0)),
        url,
    )


def iter_requests(items, folder=""):
    for node in items:
        if "request" in node:
            yield folder, node
        for child in node.get("item", []) or []:
            yield from iter_requests([child], node.get("name", folder))


def rediscover(http, base, variables) -> None:
    """Point ``active_gateway_id`` at a gateway that currently exists.

    The suite mutates gateway state (create with an empty body -> 400, delete
    -> 200), so the id the templates hold can go stale mid-run. Re-resolving
    before each id-templated request is simpler and more robust than retrying.
    """
    try:
        listing = http.get(f"{base}/api/v1/gateways").json()
        gateways = listing.get("gateways", [])
        ids = [g["id"] for g in gateways]
        runtime = [g["id"] for g in gateways if g["source"] == "runtime"]
        if variables.get("active_gateway_id") not in ids:
            if runtime:
                variables["active_gateway_id"] = runtime[0]
            elif ids:
                variables["active_gateway_id"] = ids[0]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--destructive", action="store_true",
        help="also run create/patch/rotate/delete/install-write",
    )
    args = parser.parse_args()

    coll = json.load(open(COLLECTION, encoding="utf-8"))
    variables = {v["key"]: v.get("value", "") for v in coll.get("variable", [])}
    variables["base_url"] = args.base

    print(f"target: {args.base}\n")

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []

    with httpx.Client(timeout=30.0) as http:
        # rotate-key / patch / delete only apply to *runtime* gateways. With
        # --destructive, provision one first so those requests have a valid
        # target; otherwise they 404 against a config-declared gateway, which is
        # correct behaviour but a useless test.
        if args.destructive:
            try:
                probe = http.get(f"{args.base}/api/v1/upstreams").json()
                keys = [k for k, s in probe.get("upstreams", {}).items()
                        if s["state"] == "ready"]
                if keys:
                    created = http.post(
                        f"{args.base}/api/v1/gateways",
                        json={"name": "postman smoke target", "upstreams": keys[:1]},
                    )
                    if created.status_code == 201:
                        data = created.json()
                        variables["active_gateway_id"] = data["id"]
                        variables["gateway_api_key"] = data["api_key"]
                        print(f"provisioned runtime gateway for mutating "
                              f"requests: {data['id']}")
                    else:
                        print(f"{YELLOW}could not provision: "
                              f"{created.status_code} {created.text[:80]}{RESET}")
            except Exception as exc:  # noqa: BLE001
                print(f"{YELLOW}could not provision a runtime gateway: {exc}{RESET}")

        rediscover(http, args.base, variables)
        if variables.get("active_gateway_id"):
            print(f"active gateway: {variables['active_gateway_id']}")
        print()

        for _folder, req in iter_requests(coll.get("item", [])):
            name = req["name"]
            request = req["request"]
            method = request.get("method", "GET")
            raw = (request.get("url") or {}).get("raw", "")
            if not raw:
                continue

            if "{{active_gateway_id}}" in raw:
                rediscover(http, args.base, variables)
            url = resolve(raw, variables)

            if name in MUTATING and not args.destructive:
                print(f"  {YELLOW}SKIP{RESET} {method:<6} {name}")
                skipped += 1
                continue

            try:
                if method == "GET":
                    resp = http.get(url)
                elif method == "POST":
                    resp = http.post(url, json={})
                elif method == "PATCH":
                    resp = http.patch(url, json={"tool_filter": ["*"]})
                elif method == "DELETE":
                    resp = http.delete(url)
                else:
                    resp = http.request(method, url)

                code = resp.status_code
                detail = str(code)

                # 4xx is often the correct answer here: 400 on a bodyless POST,
                # 409 deleting a config-declared gateway. Only 404/405 and 5xx
                # indicate a collection or server problem.
                ok = code < 500 and code not in (404, 405)
                if code == 404:
                    detail = "404 (endpoint missing?)"
                elif code >= 500:
                    detail = f"{code} {resp.text[:60]}"

                if name == "Create gateway" and code == 201:
                    data = resp.json()
                    variables["active_gateway_id"] = data["id"]
                    variables["gateway_api_key"] = data["api_key"]
                    detail = f"201 id={data['id']}"
                elif code in (400, 409):
                    detail = f"{code} (expected for this request shape)"

                colour = GREEN if ok else RED
                print(f"  {colour}{'PASS' if ok else 'FAIL'}{RESET} "
                      f"{method:<6} {name:<44} {DIM}{detail}{RESET}")
                if ok:
                    passed += 1
                else:
                    failed += 1
                    failures.append((name, detail))
            except Exception as exc:  # noqa: BLE001
                print(f"  {RED}FAIL{RESET} {method:<6} {name:<44} {DIM}{exc}{RESET}")
                failed += 1
                failures.append((name, str(exc)))

    print()
    print("=" * 74)
    print(f"{passed} passed, {failed} failed, {skipped} skipped (mutating)")
    if failures:
        print("\nFAILURES:")
        for n, d in failures:
            print(f"  - {n}  ({d})")
    print("=" * 74)
    if skipped:
        print("\nRe-run with --destructive to include the mutating requests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
