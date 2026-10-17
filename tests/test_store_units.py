"""Unit tests for the gateway store and agent-config rendering.

No server, no network. Run:  python -m pytest tests/test_store_units.py -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcprelay import agentconfig  # noqa: E402
from mcprelay.store import GatewayStore, StoreError, generate_api_key  # noqa: E402

UPSTREAMS = {"memory", "filesystem", "github"}


@pytest.fixture()
def store(tmp_path):
    s = GatewayStore(str(tmp_path / "gateways.json"), allowed_upstreams=UPSTREAMS)
    s.load()
    return s


# ------------------------------------------------------------------ store


class TestKeyGeneration:
    def test_keys_are_unique_and_prefixed(self):
        keys = {generate_api_key() for _ in range(500)}
        assert len(keys) == 500, "collision in 500 keys"
        assert all(k.startswith("mcr_") for k in keys)

    def test_keys_are_long_enough_to_be_unguessable(self):
        # 32 url-safe bytes ~ 256 bits.
        assert len(generate_api_key()) >= 40


class TestCreate:
    def test_create_assigns_id_key_and_timestamp(self, store):
        gw = store.create(name="coding", upstreams=["memory"])
        assert gw.id and len(gw.id) == 12
        assert gw.api_key.startswith("mcr_")
        assert gw.created_at
        assert gw.tool_filter == ["*"]

    def test_create_persists(self, store, tmp_path):
        gw = store.create(name="coding", upstreams=["memory"])
        reloaded = GatewayStore(str(tmp_path / "gateways.json"), allowed_upstreams=UPSTREAMS)
        reloaded.load()
        assert [g.id for g in reloaded.list()] == [gw.id]
        assert reloaded.get(gw.id).api_key == gw.api_key

    def test_rejects_empty_name(self, store):
        with pytest.raises(StoreError):
            store.create(name="   ", upstreams=["memory"])

    def test_rejects_no_upstreams(self, store):
        with pytest.raises(StoreError):
            store.create(name="x", upstreams=[])

    def test_rejects_unknown_upstream(self, store):
        with pytest.raises(StoreError) as exc:
            store.create(name="x", upstreams=["memory", "ghost"])
        assert "ghost" in str(exc.value)

    def test_public_view_hides_the_key(self, store):
        gw = store.create(name="coding", upstreams=["memory"])
        assert "api_key" not in gw.to_public()
        assert gw.to_public(include_key=True)["api_key"] == gw.api_key

    def test_two_gateways_get_different_keys(self, store):
        a = store.create(name="a", upstreams=["memory"])
        b = store.create(name="b", upstreams=["memory"])
        assert a.api_key != b.api_key


class TestRotate:
    def test_rotate_changes_the_key(self, store):
        gw = store.create(name="x", upstreams=["memory"])
        before = gw.api_key
        after = store.rotate_key(gw.id).api_key
        assert after != before
        assert store.get(gw.id).api_key == after

    def test_rotate_persists(self, store, tmp_path):
        gw = store.create(name="x", upstreams=["memory"])
        rotated = store.rotate_key(gw.id).api_key
        reloaded = GatewayStore(str(tmp_path / "gateways.json"), allowed_upstreams=UPSTREAMS)
        reloaded.load()
        assert reloaded.get(gw.id).api_key == rotated

    def test_rotate_unknown_gateway(self, store):
        with pytest.raises(StoreError):
            store.rotate_key("nope")


class TestDelete:
    def test_delete_removes(self, store):
        gw = store.create(name="x", upstreams=["memory"])
        store.delete(gw.id)
        assert store.get(gw.id) is None

    def test_delete_persists(self, store, tmp_path):
        gw = store.create(name="x", upstreams=["memory"])
        store.delete(gw.id)
        reloaded = GatewayStore(str(tmp_path / "gateways.json"), allowed_upstreams=UPSTREAMS)
        reloaded.load()
        assert reloaded.list() == []

    def test_delete_unknown(self, store):
        with pytest.raises(StoreError):
            store.delete("nope")


class TestUpdateTools:
    def test_update_allow_list(self, store):
        gw = store.create(name="x", upstreams=["memory"])
        updated = store.update_tools(gw.id, tool_filter=["!memory.delete_*", "*"])
        assert updated.tool_filter == ["!memory.delete_*", "*"]

    def test_update_rejects_empty_filter(self, store):
        gw = store.create(name="x", upstreams=["memory"])
        with pytest.raises(StoreError):
            store.update_tools(gw.id, tool_filter=[])


class TestResilience:
    def test_corrupt_state_file_starts_empty(self, tmp_path):
        path = tmp_path / "gateways.json"
        path.write_text("{ not json", encoding="utf-8")
        s = GatewayStore(str(path), allowed_upstreams=UPSTREAMS)
        s.load()
        assert s.list() == []

    def test_malformed_record_is_skipped(self, tmp_path):
        path = tmp_path / "gateways.json"
        good = GatewayStore(str(path), allowed_upstreams=UPSTREAMS)
        good.load()
        gw = good.create(name="keep", upstreams=["memory"])

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["gateways"].append({"id": "bad", "unexpected": "fields"})
        path.write_text(json.dumps(payload), encoding="utf-8")

        reloaded = GatewayStore(str(path), allowed_upstreams=UPSTREAMS)
        reloaded.load()
        assert [g.id for g in reloaded.list()] == [gw.id]

    def test_state_written_atomically(self, store, tmp_path):
        store.create(name="x", upstreams=["memory"])
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert not leftovers, f"temp file left behind: {leftovers}"


# ---------------------------------------------------------- agent config


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("My Gateway", "my_gateway"),
            ("  spaced  out  ", "spaced_out"),
            ("Weird-Chars!@#", "weird_chars"),
            ("already_fine", "already_fine"),
            ("", "mcprelay"),
            ("!!!", "mcprelay"),
        ],
    )
    def test_slug(self, raw, expected):
        assert agentconfig.slugify(raw) == expected


class TestRender:
    URL = "http://127.0.0.1:8787/gateway/abc123/mcp"
    KEY = "mcr_test"

    def test_generic_shape(self):
        r = agentconfig.render("generic", "my gw", self.URL, self.KEY)
        entry = r.config["mcpServers"][r.server_key]
        assert entry["url"] == self.URL
        assert entry["headers"]["X-API-Key"] == self.KEY

    def test_opencode_uses_mcp_and_type_remote(self):
        r = agentconfig.render("opencode", "gw", self.URL, self.KEY)
        entry = r.config["mcp"][r.server_key]
        assert entry["type"] == "remote"
        assert entry["enabled"] is True
        assert "mcpServers" not in r.config

    def test_vscode_uses_servers(self):
        r = agentconfig.render("vscode", "gw", self.URL, self.KEY)
        assert "servers" in r.config
        assert "mcpServers" not in r.config
        assert r.config["servers"][r.server_key]["type"] == "http"

    def test_radar_appends_path(self):
        r = agentconfig.render("cursor", "gw", self.URL, self.KEY, radar=True)
        assert r.config["mcpServers"][r.server_key]["url"].endswith("/radar")

    def test_claude_desktop_flags_unsupported_for_http(self):
        r = agentconfig.render("claude_desktop", "gw", self.URL, self.KEY)
        assert r.supported is False
        assert "stdio" in r.reason

    def test_unknown_client_raises(self):
        with pytest.raises(KeyError):
            agentconfig.render("emacs", "gw", self.URL, self.KEY)

    @pytest.mark.parametrize(
        "client", ["generic", "opencode", "cursor", "claude_code", "windsurf", "vscode"]
    )
    def test_every_supported_client_carries_url_and_key(self, client):
        r = agentconfig.render(client, "gw", self.URL, self.KEY)
        blob = json.dumps(r.config)
        assert self.URL in blob and self.KEY in blob

    @pytest.mark.parametrize("client", ["generic", "claude_desktop", "opencode", "vscode"])
    def test_stdio_bridge_shapes(self, client):
        r = agentconfig.render_stdio_bridge(client, "gw", self.URL, self.KEY)
        blob = json.dumps(r.config)
        assert "mcp-remote" in blob
        assert f"X-API-Key: {self.KEY}" in blob
        assert "127.0.0.1:8787" in blob


class TestJsonc:
    def test_strips_line_and_block_comments(self):
        raw = '// c1\n{"a": 1, /* c2 */ "b": 2}\n'
        doc, report = agentconfig._strip_jsonc(raw), None
        assert json.loads(doc) == {"a": 1, "b": 2}

    def test_load_existing_reports_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            open(p, "w").write('{"a": 1}')
            doc, report = agentconfig.load_existing(p)
            assert doc == {"a": 1}
            assert report == {"stripped": False, "format": "json"}

    def test_load_existing_reports_jsonc_stripping(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.jsonc")
            open(p, "w").write('// note\n{"a": 1,}\n')
            doc, report = agentconfig.load_existing(p)
            assert doc == {"a": 1}
            assert report["stripped"] is True
            assert report["format"] == "jsonc"

    def test_slashes_inside_strings_survive(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            open(p, "w").write('{"url": "http://x/y", "n": "a // b"}')
            doc, report = agentconfig.load_existing(p)
            assert doc["url"] == "http://x/y"
            assert doc["n"] == "a // b"
            assert report["stripped"] is False

    def test_unparseable_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            open(p, "w").write("{ not json")
            with pytest.raises(ValueError):
                agentconfig.load_existing(p)

    def test_missing_file_is_empty(self):
        doc, report = agentconfig.load_existing("/definitely/not/here.json")
        assert doc == {}


class TestWrite:
    def test_creates_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "mcp.json")
            r = agentconfig.render("generic", "gw", "http://h/mcp", "k")
            out = agentconfig.write_config(r, p)
            assert out["created"] is True
            assert out["backup_path"] is None
            assert json.load(open(p, encoding="utf-8"))["mcpServers"]

    def test_backs_up_before_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            open(p, "w").write('{"keep": 1}')
            r = agentconfig.render("generic", "gw", "http://h/mcp", "k")
            out = agentconfig.write_config(r, p)
            assert out["backup_path"] and os.path.exists(out["backup_path"])
            assert json.load(open(out["backup_path"], encoding="utf-8")) == {"keep": 1}

    def test_preserves_unrelated_keys_and_servers(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            open(p, "w").write(
                '{"top": {"a": 1}, "mcpServers": {"existing": {"url": "http://old"}}}'
            )
            r = agentconfig.render("generic", "newname", "http://h/mcp", "k")
            agentconfig.write_config(r, p)
            doc = json.load(open(p, encoding="utf-8"))
            assert doc["top"] == {"a": 1}
            assert "existing" in doc["mcpServers"]
            assert "newname" in doc["mcpServers"]

    def test_replaces_same_key_rather_than_duplicating(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            r = agentconfig.render("generic", "gw", "http://old/mcp", "oldkey")
            agentconfig.write_config(r, p)
            r2 = agentconfig.render("generic", "gw", "http://new/mcp", "newkey")
            agentconfig.write_config(r2, p)
            doc = json.load(open(p, encoding="utf-8"))
            assert len(doc["mcpServers"]) == 1
            assert doc["mcpServers"]["gw"]["headers"]["X-API-Key"] == "newkey"

    def test_opencode_merge_keeps_other_servers(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "opencode.json")
            open(p, "w").write('{"mcp": {"rea": {"type": "local"}}, "theme": "x"}')
            r = agentconfig.render("opencode", "gw", "http://h/mcp", "k")
            agentconfig.write_config(r, p)
            doc = json.load(open(p, encoding="utf-8"))
            assert "rea" in doc["mcp"]
            assert doc["theme"] == "x"

    def test_unparseable_target_refuses_to_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            open(p, "w").write("{ broken")
            r = agentconfig.render("generic", "gw", "http://h/mcp", "k")
            with pytest.raises(ValueError):
                agentconfig.write_config(r, p)
            assert open(p, encoding="utf-8").read() == "{ broken"

    def test_missing_default_path_errors(self):
        r = agentconfig.render("claude_desktop", "gw", "http://h/mcp", "k")
        with pytest.raises(ValueError):
            agentconfig.write_config(r, None)
