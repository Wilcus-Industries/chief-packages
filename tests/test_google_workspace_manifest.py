"""The google-workspace manifest: static shape + validation through chief
core's *real* MCP-manifest loader.

The loader tests skip (loudly) only when no core checkout is reachable; on a dev
machine with chief core checked out they must actually run — that is the whole
point of the slice (proving the manifest validates against the real code, not a
reimplementation of it).
"""

from __future__ import annotations

import pytest
import yaml
from conftest import repo_root

MANIFEST_PATH = repo_root() / "google-workspace" / "manifest.yaml"


def _load() -> dict[str, object]:
    data = yaml.safe_load(MANIFEST_PATH.read_text())
    assert isinstance(data, dict)
    return data


def test_manifest_parses_as_yaml() -> None:
    data = _load()
    for key in ("name", "description", "mcp_servers", "config_keys", "secrets"):
        assert key in data, f"manifest missing top-level key: {key}"
    assert data["name"] == MANIFEST_PATH.parent.name == "google-workspace"


def test_manifest_declares_stdio_command() -> None:
    """The upstream server is wrapped as a stdio ``command`` (not a URL), and
    the write-gate metadata (config_keys/secrets/python_deps) is present."""
    data = _load()
    servers = data["mcp_servers"]
    assert isinstance(servers, dict) and servers, "mcp_servers must be non-empty"
    entry = servers["google-workspace"]
    assert isinstance(entry, dict)
    assert "url" not in entry, "server is stdio — must not declare a url"
    command = entry["command"]
    assert isinstance(command, list) and command, "command must be a non-empty list"
    assert all(isinstance(part, str) and part for part in command)
    assert data["config_keys"] == ["google_workspace"]
    assert data["secrets"] == ["google_workspace_credentials"]
    assert data["python_deps"] == []


def test_manifest_validates_through_real_loader(core_available: bool) -> None:
    if not core_available:
        pytest.skip(
            "chief core checkout not found (set $CHIEF_CORE or clone next to "
            "chief-packages) — cannot run the REAL manifest loader; this test "
            "must actually run on a dev machine, not skip"
        )
    from chief.mcp_manifest import parse_mcp_servers, validate_mcp_servers

    servers = _load()["mcp_servers"]
    assert validate_mcp_servers(MANIFEST_PATH, servers) == []

    specs = parse_mcp_servers(servers)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "google-workspace"
    assert spec.url is None
    assert isinstance(spec.command, tuple)
    assert len(spec.command) > 0
    assert all(isinstance(part, str) and part for part in spec.command)


def test_malformed_mcp_servers_rejected(core_available: bool) -> None:
    """Negative proof the real validator (not a stub) runs: a server setting
    BOTH url and command must be reported as a problem."""
    if not core_available:
        pytest.skip(
            "chief core checkout not found — cannot exercise the REAL validator"
        )
    from chief.mcp_manifest import validate_mcp_servers

    malformed = {"gw": {"url": "https://example.test", "command": ["uvx", "x"]}}
    problems = validate_mcp_servers(MANIFEST_PATH, malformed)
    assert problems, "validator must reject a server that sets both url and command"
