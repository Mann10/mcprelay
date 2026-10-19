"""Env-reference expansion for runtime (TUI / control-plane) upstreams.

Semantics (Option B): the spec is persisted verbatim with ``${env:...}``
references — secrets never reach ``var/upstreams.json`` — and resolved when
the spec becomes a live ``UpstreamConfig``, at create time for the immediate
connect and at startup for restored ones. A reference to an unset variable
is a clean 400 naming the variable, and nothing broken is persisted.

Run:  python -m pytest tests/test_runtime_upstream_env.py -q
      (or:  python tests/test_runtime_upstream_env.py)
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from mcprelay.config import ConfigError, GatewayAppConfig, expand_spec  # noqa: E402

VAR = "MCPR_UNIT_TEST_TOKEN"
VAR_MISSING = "MCPR_UNIT_TEST_DEFINITELY_UNSET"
TOKEN = "unit-test-secret-123"


# ----------------------------------------------------------------- pure logic


class TestExpandSpec:
    def test_resolves_nested_references(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VAR, TOKEN)
        spec = {
            "transport": "streamable-http",
            "url": "https://example.invalid/mcp",
            "headers": {"Authorization": f"${{{VAR}}}"},
        }
        resolved = expand_spec(spec)
        assert resolved["headers"]["Authorization"] == TOKEN

    def test_reference_survives_in_the_original(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # the caller's spec keeps ${env:...} — that is what gets persisted
        monkeypatch.setenv(VAR, TOKEN)
        spec = {"headers": {"Authorization": f"${{{VAR}}}"}}
        expand_spec(spec)
        assert spec["headers"]["Authorization"] == f"${{{VAR}}}"

    def test_default_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCPR_UNIT_TEST_ALSO_UNSET", raising=False)
        spec = {"headers": {"X-Tier": "${MCPR_UNIT_TEST_ALSO_UNSET:beta}"}}
        assert expand_spec(spec)["headers"]["X-Tier"] == "beta"

    def test_missing_variable_raises_naming_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(VAR_MISSING, raising=False)
        spec = {"headers": {"Authorization": f"${{{VAR_MISSING}}}"}}
        with pytest.raises(ConfigError) as excinfo:
            expand_spec(spec)
        assert VAR_MISSING in str(excinfo.value)

    def test_literals_pass_through_unchanged(self) -> None:
        spec = {"transport": "stdio", "args": ["-y", "x"], "env": {"A": "b"}}
        assert expand_spec(spec) == spec

    def test_never_rejects_non_string_shapes(self) -> None:
        spec = {"headers": {"X-N": 123, "X-B": True}}
        assert expand_spec(spec)["headers"]["X-N"] == 123


# ------------------------------------------------------- server behaviour


def _make_client(tmp_path: Path):
    from fastapi.testclient import TestClient
    from mcprelay.app import build_app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
        server:
          host: 127.0.0.1
          port: 0
        gateways_state_path: {(tmp_path / "gateways.json").as_posix()}
        upstreams_state_path: {(tmp_path / "upstreams.json").as_posix()}
        upstreams:
          dummy:
            transport: stdio
            command: cmd
            args: ["/c", "exit"]
            enabled: false
        gateways:
          dev:
            api_key: unit-test-key
            upstreams: [dummy]
        """
        ),
        encoding="utf-8",
    )
    app = build_app(GatewayAppConfig.load(str(cfg)))
    return TestClient(app)


def _persisted(tmp_path: Path) -> list[dict]:
    path = tmp_path / "upstreams.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("upstreams", [])


class TestCreateUpstreamEnvRefs:
    def test_reference_resolved_for_connect_and_persisted_as_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VAR, TOKEN)
        with _make_client(tmp_path) as client:
            r = client.post(
                "/api/v1/upstreams",
                json={
                    "key": "tavily",
                    "transport": "streamable-http",
                    "url": "http://127.0.0.1:9/mcp",  # connect fails fast; 201 anyway
                    "headers": {"Authorization": f"${{env:{VAR}}}"},
                },
            )
            assert r.status_code == 201, r.text
            assert r.json()["key"] == "tavily"

        # the reference — not the secret — is what reached disk
        records = _persisted(tmp_path)
        assert [rec["key"] for rec in records] == ["tavily"]
        raw = json.dumps(records)
        assert f"${{env:{VAR}}}" in raw
        assert TOKEN not in raw

    def test_missing_variable_is_a_clean_400_and_nothing_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(VAR_MISSING, raising=False)
        with _make_client(tmp_path) as client:
            r = client.post(
                "/api/v1/upstreams",
                json={
                    "key": "broken",
                    "transport": "streamable-http",
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": f"${{{VAR_MISSING}}}"},
                },
            )
            assert r.status_code == 400
            assert VAR_MISSING in r.json()["error"]
        assert _persisted(tmp_path) == []


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-q"])
