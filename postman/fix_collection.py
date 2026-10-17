"""Repair and validate the Postman collection in place.

Two classes of problem this fixes:

1. Missing ``url`` blocks on requests that only had a method + body. Postman
   tolerates it when hand-editing but the request will not send.
2. ``{{gateway_url}}`` template references, which was never a declared variable.

Run:  python fix_collection.py      (repairs, then validates)
      python fix_collection.py --check-only
"""

from __future__ import annotations

import json
import os
import re
import sys

COLLECTION = "mcprelay.postman_collection.json"
ENVIRONMENT = "local.postman_environment.json"

# Requests whose method+body were authored without a url block.
URL_FIXES = {
    "Create gateway": {
        "raw": "{{base_url}}/api/v1/gateways",
        "host": ["{{base_url}}"],
        "path": ["api", "v1", "gateways"],
    },
    "Patch allow-list": {
        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}",
        "host": ["{{base_url}}"],
        "path": ["api", "v1", "gateways", "{{active_gateway_id}}"],
    },
    "Install client config (DRY RUN)": {
        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}/install/{{client_id}}",
        "host": ["{{base_url}}"],
        "path": ["api", "v1", "gateways", "{{active_gateway_id}}", "install", "{{client_id}}"],
    },
    "Install client config (WRITE)": {
        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}/install/{{client_id}}",
        "host": ["{{base_url}}"],
        "path": ["api", "v1", "gateways", "{{active_gateway_id}}", "install", "{{client_id}}"],
    },
    "Install to Claude Desktop (stdio bridge)": {
        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}/install/claude_desktop",
        "host": ["{{base_url}}"],
        "path": [
            "api", "v1", "gateways", "{{active_gateway_id}}",
            "install", "claude_desktop",
        ],
    },
}


def iter_requests(items):
    for node in items:
        if "request" in node:
            yield node
        for child in node.get("item", []) or []:
            yield from iter_requests([child])


def main() -> int:
    check_only = "--check-only" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    coll_path = os.path.join(here, COLLECTION)

    coll = json.load(open(coll_path, encoding="utf-8"))
    changed = False

    # ---- 1. add missing url blocks -------------------------------------
    for req in iter_requests(coll.get("item", [])):
        name = req.get("name", "")
        request = req.get("request", {})
        if "url" in request:
            continue
        fix = URL_FIXES.get(name)
        if fix:
            request["url"] = dict(fix)
            changed = True
            print(f"  added url to {name!r}: {fix['raw']}")
        else:
            print(f"  WARNING no url fix known for {name!r}")

    # ---- 2. declare any undeclared template variables -------------------
    declared = {v["key"] for v in coll.get("variable", [])}
    used = set(re.findall(r"\{\{([a-z_][a-z0-9_]*)\}\}", json.dumps(coll)))
    for missing in sorted(used - declared):
        coll.setdefault("variable", []).append(
            {"key": missing, "value": "", "type": "string"}
        )
        changed = True
        print(f"  declared missing variable: {missing}")

    if changed and not check_only:
        with open(coll_path, "w", encoding="utf-8") as fh:
            json.dump(coll, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("  collection rewritten")
    elif changed:
        print("  --check-only: not writing")

    # ---- 3. re-validate --------------------------------------------------
    coll = json.load(open(coll_path, encoding="utf-8"))
    env = json.load(open(os.path.join(here, ENVIRONMENT), encoding="utf-8"))

    print()
    print(f"collection : {COLLECTION}  ({os.path.getsize(coll_path):,} bytes)")
    print(f"environment: {ENVIRONMENT}  ({os.path.getsize(os.path.join(here, ENVIRONMENT)):,} bytes)")
    print(f"base_url   : {env['values'][0]['value']}")
    print()

    declared = {v["key"] for v in coll.get("variable", [])}
    used = set(re.findall(r"\{\{([a-z_][a-z0-9_]*)\}\}", json.dumps(coll)))
    unresolved = used - declared
    print(f"variables used     : {', '.join(sorted(used))}")
    print(f"unresolved refs    : {', '.join(sorted(unresolved)) or 'none'}")

    total = 0
    problems = []
    for folder in coll.get("item", []):
        print()
        print(f"{folder['name']}")
        if folder.get("description"):
            first = folder["description"].strip().splitlines()[0]
            print(f"  {first}")
        for req in folder.get("item", []):
            total += 1
            request = req.get("request", {})
            method = request.get("method", "?")
            url = (request.get("url") or {}).get("raw", "(none)")
            missing = [k for k in ("method", "url") if k not in request]
            if missing:
                problems.append((req["name"], missing))
            print(f"    {method:<6} {req['name']}")
            print(f"           {url}")
            print(
                f"           {len(req.get('response', []) or [])} saved example(s)"
                + (f"   !! MISSING {missing}" if missing else "")
            )

    print()
    print(f"total requests: {total}")
    print(f"env variables : {len(env['values'])}")
    if unresolved or problems:
        print()
        print("PROBLEMS:")
        for ref in sorted(unresolved):
            print(f"  unresolved variable: {{{{{ref}}}}}")
        for name, missing in problems:
            print(f"  {name}: missing {missing}")
        return 1
    print("RESULT: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
