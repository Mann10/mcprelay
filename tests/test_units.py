"""Unit tests for the pure logic: namespacing, allow-list matching, config.

No network, no subprocesses. Run:  python -m pytest tests/test_units.py -q
"""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcprelay.config import ConfigError, GatewayAppConfig, expand_env  # noqa: E402
from mcprelay.naming import (  # noqa: E402
    NamingError,
    match_filter,
    qualify,
    split,
    truncate,
    validate_upstream_key,
)
from mcprelay.registry import build_tool, strict_surfaces, to_wire_tool  # noqa: E402
from mcprelay.upstream import UpstreamTool, describe_error  # noqa: E402


# ----------------------------------------------------------------- namespacing


class TestNaming:
    def test_qualify_and_split_round_trip(self):
        assert qualify("github", "create_issue") == "github.create_issue"
        assert split("github.create_issue") == ("github", "create_issue")

    def test_split_uses_first_separator_only(self):
        # An upstream whose tool name itself contains a dot must still parse.
        assert split("server.do.thing") == ("server", "do.thing")

    def test_split_rejects_unnamespaced(self):
        with pytest.raises(NamingError):
            split("bare_tool_name")

    def test_split_rejects_empty_side(self):
        for bad in (".tool", "upstream.", "."):
            with pytest.raises(NamingError):
                split(bad)

    @pytest.mark.parametrize("key", ["github", "my-app", "a_b", "A1"])
    def test_valid_keys(self, key):
        assert validate_upstream_key(key) == key

    @pytest.mark.parametrize("key", ["has.dot", "has space", "has/slash", ""])
    def test_invalid_keys_rejected(self, key):
        with pytest.raises(NamingError):
            validate_upstream_key(key)

    def test_key_with_dot_would_be_ambiguous(self):
        # This is the reason the restriction exists.
        with pytest.raises(NamingError):
            validate_upstream_key("my.app")


# ------------------------------------------------------------------- filtering


class TestFilter:
    def test_star_allows_all(self):
        assert match_filter("github.create_issue", ("*",)).included

    def test_no_match_is_denied(self):
        # Strict by default: no implicit allow-all.
        assert not match_filter("github.create_issue", ("memory.*",)).included

    def test_negation_then_star(self):
        patterns = ("!memory.delete_*", "*")
        assert not match_filter("memory.delete_entities", patterns).included
        assert match_filter("memory.read_graph", patterns).included
        assert match_filter("github.create_issue", patterns).included

    def test_first_match_wins_allow_then_star(self):
        # A narrow allow ahead of "*" does NOT create a deny-list: "*" still
        # matches everything else. First-match-wins, not gitignore precedence.
        patterns = ("github.create_issue", "*")
        assert match_filter("github.create_issue", patterns).included
        assert match_filter("github.delete_repo", patterns).included

    def test_first_match_wins_deny_then_allow(self):
        # Here the narrow rule is a negation, so it wins for its own tool.
        patterns = ("!github.delete_*", "*")
        assert not match_filter("github.delete_repo", patterns).included
        assert match_filter("github.create_issue", patterns).included

    def test_trailing_negation_is_unreachable_under_first_match(self):
        # "*" matches first, so the later "!github.*" never runs. This is the
        # sharp edge of first-match-wins: put negations FIRST.
        patterns = ("*", "!github.*")
        assert match_filter("github.create_issue", patterns).included
        assert match_filter("memory.read_graph", patterns).included

    def test_negation_first_is_the_correct_deny_pattern(self):
        patterns = ("!github.*", "*")
        assert not match_filter("github.create_issue", patterns).included
        assert match_filter("memory.read_graph", patterns).included

    def test_reports_which_pattern_matched(self):
        outcome = match_filter("memory.delete_x", ("!memory.delete_*", "*"))
        assert outcome.matched and outcome.pattern == "!memory.delete_*"


# ------------------------------------------------------------------- truncate


class TestTruncate:
    def test_short_text_untouched(self):
        assert truncate("hello", 100) == "hello"

    def test_long_text_shortened(self):
        out = truncate("word " * 100, 20)
        assert len(out) <= 30 and out.endswith("...")

    def test_none_passthrough(self):
        assert truncate(None, 10) is None

    def test_zero_limit_means_untouched(self):
        text = "x" * 500
        assert truncate(text, 0) == text


# ------------------------------------------------------------- error flattening


class TestDescribeError:
    def test_plain_exception(self):
        assert describe_error(ValueError("boom")) == "ValueError: boom"

    def test_unwraps_exception_group(self):
        # anyio wraps transport failures; the leaf message is what matters.
        group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                               [OSError("connect refused")])
        out = describe_error(group)
        assert "connect refused" in out
        assert "TaskGroup" not in out

    def test_dedupes_identical_leaves(self):
        group = ExceptionGroup("x", [ValueError("same"), ValueError("same")])
        assert describe_error(group) == "ValueError: same"


# ----------------------------------------------------------------------- config


class TestConfig:
    def test_expand_env(self, monkeypatch):
        monkeypatch.setenv("MCPRELAY_TEST_VAR", "value123")
        assert expand_env("${env:MCPRELAY_TEST_VAR}") == "value123"
        assert expand_env("${MCPRELAY_TEST_VAR}") == "value123"
        assert expand_env("prefix-${MCPRELAY_TEST_VAR}-suffix") == "prefix-value123-suffix"

    def test_expand_env_default(self):
        assert expand_env("${NOT_SET_AT_ALL:fallback}") == "fallback"

    def test_expand_env_missing_is_error(self):
        with pytest.raises(ConfigError) as exc:
            expand_env("${definitely_not_set_12345}")
        assert "definitely_not_set_12345" in str(exc.value)


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "c.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


BASE = """
server:
  host: 127.0.0.1
  port: 8787
upstreams:
  memory:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]
gateways:
  dev:
    api_key: "k1"
    upstreams: [memory]
"""


class TestConfigValidation:
    def test_minimal_config_loads(self, tmp_path):
        cfg = GatewayAppConfig.load(_write(tmp_path, BASE))
        assert cfg.server.port == 8787
        assert cfg.gateways["dev"].path == "/gateway/dev/mcp"
        assert cfg.gateways["dev"].radar_path == "/gateway/dev/radar"

    def test_rejects_unknown_upstream_reference(self, tmp_path):
        body = BASE.replace("upstreams: [memory]", "upstreams: [memory, ghost]")
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "ghost" in str(exc.value)

    def test_rejects_gateway_without_upstreams(self, tmp_path):
        body = BASE.replace("upstreams: [memory]", "upstreams: []")
        with pytest.raises(ConfigError):
            GatewayAppConfig.load(_write(tmp_path, body))

    def test_rejects_missing_api_key(self, tmp_path):
        body = BASE.replace('api_key: "k1"', "description: no key here")
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "api_key" in str(exc.value)

    def test_rejects_duplicate_api_keys(self, tmp_path):
        body = BASE + """
  second:
    api_key: "k1"
    upstreams: [memory]
"""
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "share the same api_key" in str(exc.value)

    def test_rejects_stdio_without_command(self, tmp_path):
        body = BASE.replace(
            '    transport: stdio\n    command: npx\n    args: ["-y", "@modelcontextprotocol/server-memory"]',
            "    transport: stdio",
        )
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "command" in str(exc.value)

    def test_rejects_http_without_url(self, tmp_path):
        # Append a broken upstream under the existing `upstreams:` mapping.
        body = BASE.replace(
            "gateways:",
            '  remote:\n    transport: streamable-http\n    url: ""\n\n  zzz:\n'
            "    transport: streamable-http\n\ngateways:",
        )
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "url" in str(exc.value)

    def test_rejects_bad_transport(self, tmp_path):
        body = BASE.replace("transport: stdio", "transport: carrier-pigeon")
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "transport" in str(exc.value)

    def test_rejects_bad_cache_scope(self, tmp_path):
        body = BASE.replace(
            'api_key: "k1"', 'api_key: "k1"\n    cache_scope: organization'
        )
        with pytest.raises(ConfigError) as exc:
            GatewayAppConfig.load(_write(tmp_path, body))
        assert "cache_scope" in str(exc.value)

    def test_header_with_scheme_but_no_secret_is_dropped(self, tmp_path):
        # "Bearer ${env:TOKEN:}" is the classic footgun: it expands to a scheme
        # with no secret, which httpx rejects with a transport-level error that
        # never mentions the missing env var. mcprelay drops it instead.
        body = BASE.replace(
            "gateways:",
            "  gh:\n"
            "    transport: streamable-http\n"
            '    url: "https://example.invalid/mcp"\n'
            "    headers:\n"
            '      Authorization: "Bearer ${NOT_SET_ANYWHERE:}"\n'
            '      X-Trace: "${NOT_SET_ANYWHERE:}"\n'
            "    enabled: false\n"
            "\ngateways:",
        )
        cfg = GatewayAppConfig.load(_write(tmp_path, body))
        assert "Authorization" not in cfg.upstreams["gh"].headers
        assert "X-Trace" not in cfg.upstreams["gh"].headers

    def test_header_with_real_credential_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REAL_TOKEN", "abc123")
        body = BASE.replace(
            "gateways:",
            "  gh:\n"
            "    transport: streamable-http\n"
            '    url: "https://example.invalid/mcp"\n'
            "    headers:\n"
            '      Authorization: "Bearer ${REAL_TOKEN}"\n'
            '      X-Api-Key: "${REAL_TOKEN}"\n'
            "    enabled: false\n"
            "\ngateways:",
        )
        cfg = GatewayAppConfig.load(_write(tmp_path, body))
        assert cfg.upstreams["gh"].headers["Authorization"] == "Bearer abc123"
        assert cfg.upstreams["gh"].headers["X-Api-Key"] == "abc123"

    def test_tool_filter_requires_nonempty_list(self, tmp_path):
        body = BASE.replace('api_key: "k1"', 'api_key: "k1"\n    tool_filter:\n      include: []')
        with pytest.raises(ConfigError):
            GatewayAppConfig.load(_write(tmp_path, body))


# ------------------------------------------------------------- wire rendering


class TestToWireTool:
    def _tool(self, name="github.create_issue", desc="Creates an issue"):
        return UpstreamTool(
            qualified_name=name,
            upstream_key="github",
            original_name="create_issue",
            schema_doc={"inputSchema": {"type": "object", "properties": {}}},
            description=desc,
        )

    def test_names_are_namespaced_and_annotated(self):
        doc = to_wire_tool(self._tool(), 400)
        assert doc["name"] == "github.create_issue"
        assert "[github]" in doc["description"]
        assert doc["inputSchema"]["type"] == "object"

    def test_description_truncated(self):
        doc = to_wire_tool(self._tool(desc="x" * 1000), 50)
        assert len(doc["description"]) < 200

    def test_missing_description_gets_synthetic_one(self):
        doc = to_wire_tool(self._tool(desc=None), 400)
        assert "[github]" in doc["description"]

    def test_other_passthrough_fields_preserved(self):
        tool = self._tool()
        tool.schema_doc["title"] = "Create issue"
        tool.schema_doc["annotations"] = {"readOnlyHint": False}
        doc = to_wire_tool(tool, 400)
        assert doc["title"] == "Create issue"
        assert doc["annotations"] == {"readOnlyHint": False}

    def test_output_schema_withheld_by_default(self):
        # Regression: proxying outputSchema to a pre-2026-07-28 client made
        # the SDK's downgrade path rewrite it into a shape its own serializer
        # rejects, failing the ENTIRE tools/list for every tool on the gateway.
        tool = self._tool()
        tool.schema_doc["outputSchema"] = {"type": ["object", "null"]}
        doc = to_wire_tool(tool, 400)
        assert "outputSchema" not in doc

    def test_output_schema_opt_in(self):
        tool = self._tool()
        tool.schema_doc["outputSchema"] = {"type": "object"}
        doc = to_wire_tool(tool, 400, include_output_schema=True)
        assert doc["outputSchema"] == {"type": "object"}


class TestBuildTool:
    """build_tool is the last line of defence before tools/list is returned.

    One malformed upstream schema must never cost the caller every other tool
    on the gateway, so this degrades the offending field/tool instead of raising.
    """

    def test_strict_surfaces_are_discovered(self):
        # Guard the premise of this whole class: the SDK really does expose
        # per-version surface models, and the legacy one is the strict one.
        surfaces = strict_surfaces()
        assert "2025-11-25" in surfaces
        assert "2026-07-28" in surfaces

    def test_valid_tool_passes_through_untouched(self):
        built, note = build_tool(
            {"name": "github.create_issue", "inputSchema": {"type": "object"}},
            protocol_version="2025-11-25",
        )
        assert note is None
        assert built.name == "github.create_issue"

    def test_returns_a_types_tool_not_a_versioned_surface_model(self):
        # Regression: validating against the strict per-version surface model
        # meant returning THAT model, which `ListToolsResult` rejects - the
        # handler then died at construction time and the client saw a bare
        # -32602 "Invalid request parameters" with no explanation.
        from mcp import types

        built, _ = build_tool(
            {"name": "github.create_issue", "inputSchema": {"type": "object"}},
            protocol_version="2025-11-25",
        )
        assert isinstance(built, types.Tool)
        # And it must actually be constructible into the result we return.
        result = types.ListToolsResult(tools=[built], ttl_ms=1000, cache_scope="private")
        assert len(result.tools) == 1

    def test_dropped_schema_result_serializes_for_both_versions(self):
        from mcp import types
        from mcp_types.methods import serialize_server_result

        built, note = build_tool({
            "name": "github.list_issues",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": ["object", "null"]},
        }, protocol_version="2025-11-25")
        assert note
        dumped = types.ListToolsResult(
            tools=[built], ttl_ms=1000, cache_scope="private"
        ).model_dump(by_alias=True, exclude_none=True)
        for version in ("2025-11-25", "2026-07-28"):
            out = serialize_server_result("tools/list", version, dumped)
            assert "outputSchema" not in out["tools"][0]

    def test_bad_output_schema_is_dropped_not_raised(self):
        # Exactly what GitHub's server emitted: a 2020-12 `type` union, which
        # the pre-2026 OutputSchema model rejects because it demands the
        # literal 'object'. `mcp.types.Tool` would have accepted this happily.
        built, note = build_tool({
            "name": "github.list_issues",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": ["object", "null"]},
        }, protocol_version="2025-11-25")
        assert built is not None, "tool must survive; only outputSchema is dropped"
        assert built.name == "github.list_issues"
        assert built.output_schema is None
        assert note and "outputSchema" in note

    def test_oneof_only_output_schema_is_dropped(self):
        built, note = build_tool({
            "name": "github.get_issue",
            "inputSchema": {"type": "object"},
            "outputSchema": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        }, protocol_version="2025-11-25")
        assert built is not None
        assert note and "outputSchema" in note

    def test_modern_version_accepts_the_union_schema(self):
        # 2026-07-28's OutputSchema is unrestricted, so the same document
        # validates there. This is why validation must be per-version.
        built, note = build_tool({
            "name": "github.list_issues",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": ["object", "null"]},
        }, protocol_version="2026-07-28")
        assert note is None
        assert built.output_schema is not None
        assert built.output_schema["type"] == ["object", "null"]

    def test_tool_without_a_name_is_dropped(self):
        built, note = build_tool(
            {"inputSchema": {"type": "object"}}, protocol_version="2025-11-25"
        )
        assert built is None
        assert note

    def test_never_raises_on_garbage(self):
        for version in ("2025-11-25", "2026-07-28", None, "not-a-version"):
            for doc in ({}, {"name": 123}, {"name": "a", "inputSchema": "nope"}, None):
                built, note = build_tool(doc, protocol_version=version)  # type: ignore[arg-type]
                assert built is None or built.name
                assert note is None or note
