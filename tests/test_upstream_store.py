"""Offline server-side tests for runtime upstream CRUD."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from mcprelay.store import StoreError, UpstreamStore


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "upstreams.json")
        store = UpstreamStore(path)

        # create + persist
        up = store.create(key="notion", spec={
            "transport": "streamable-http",
            "url": "https://mcp.notion.com/mcp",
            "headers": {"Authorization": "${env:NOTION_KEY}"},
        })
        assert up.key == "notion"
        reloaded = json.loads(Path(path).read_text(encoding="utf-8"))
        assert reloaded["upstreams"][0]["key"] == "notion", reloaded

        # restores from disk
        store2 = UpstreamStore(path)
        store2.load()
        assert store2.get("notion") is not None
        assert store2.get("notion").spec["url"].startswith("https://mcp.notion")

        # duplicate key
        try:
            store2.create(key="notion", spec={"transport": "stdio", "command": "x"})
            raise AssertionError("duplicate should fail")
        except StoreError:
            pass

        # bad key charset
        try:
            store2.create(key="bad key!", spec={"transport": "stdio", "command": "x"})
            raise AssertionError("bad charset should fail")
        except StoreError as exc:
            assert "key" in str(exc).lower(), str(exc)

        # bad transport
        try:
            store2.create(key="junk", spec={"transport": "carrier-pigeon"})
            raise AssertionError("bad transport should fail")
        except StoreError as exc:
            assert "transport" in str(exc).lower(), str(exc)

        # stdio requires a command
        try:
            store2.create(key="junk", spec={"transport": "stdio"})
            raise AssertionError("missing command should fail")
        except StoreError as exc:
            assert "command" in str(exc).lower(), str(exc)

        # http requires a url
        try:
            store2.create(key="junk", spec={"transport": "sse"})
            raise AssertionError("missing url should fail")
        except StoreError as exc:
            assert "url" in str(exc).lower(), str(exc)

        # delete removes and persists
        store2.delete("notion")
        assert store2.get("notion") is None
        assert json.loads(Path(path).read_text(encoding="utf-8"))["upstreams"] == []

        # delete unknown
        try:
            store2.delete("ghost")
            raise AssertionError("unknown delete should fail")
        except StoreError:
            pass

    print("UPSTREAM STORE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
