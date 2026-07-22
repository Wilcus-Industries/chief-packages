"""Static invariants that must hold for EVERY package manifest in the repo.

Parametrized over discovered manifests so a newly added package is covered
automatically. The invariants checked here (name/description present, name ==
dir, declared skills exist on disk) were verified to hold across all existing
packages; the mcp_servers block is validated through chief core's real loader
whenever it is non-empty (most existing packages declare an empty ``{}``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import repo_root


def _discover_manifests() -> list[Path]:
    return sorted(repo_root().glob("*/manifest.yaml"))


def _skill_within_package(manifest_path: Path, entry: str) -> bool:
    """True iff a ``skills:`` entry resolves inside its own package dir.

    This repo later becomes an install source, so an entry like
    ``../other-pkg/skills/x`` reaching outside the package is rejected.
    """
    pkg_dir = manifest_path.parent.resolve()
    skill_dir = (manifest_path.parent / entry).resolve()
    return skill_dir == pkg_dir or pkg_dir in skill_dir.parents


MANIFESTS = _discover_manifests()


def test_repo_has_packages() -> None:
    # Guards against a broken discovery glob silently parametrizing to nothing.
    assert MANIFESTS, "no */manifest.yaml discovered under the repo root"


@pytest.mark.parametrize(
    "manifest_path", MANIFESTS, ids=[p.parent.name for p in MANIFESTS]
)
def test_manifest_is_well_formed(manifest_path: Path) -> None:
    data = yaml.safe_load(manifest_path.read_text())
    assert isinstance(data, dict), f"{manifest_path}: manifest must be a mapping"

    name = data.get("name")
    assert isinstance(name, str) and name.strip(), f"{manifest_path}: needs a name"

    desc = data.get("description")
    assert isinstance(desc, str) and desc.strip(), (
        f"{manifest_path}: needs a description"
    )

    assert name == manifest_path.parent.name, (
        f"{manifest_path}: name '{name}' != dir '{manifest_path.parent.name}'"
    )

    skills = data.get("skills")
    if skills:
        assert isinstance(skills, list)
        for entry in skills:
            # A declared skill must live inside its own package dir (this repo
            # later becomes an install source — a ``../other-pkg/...`` entry is
            # rejected, not silently accepted).
            assert _skill_within_package(manifest_path, str(entry)), (
                f"{manifest_path}: declared skill '{entry}' resolves outside "
                "the package dir"
            )
            skill_dir = manifest_path.parent / str(entry)
            assert skill_dir.is_dir(), (
                f"{manifest_path}: declared skill '{entry}' not found on disk"
            )


@pytest.mark.parametrize(
    "manifest_path", MANIFESTS, ids=[p.parent.name for p in MANIFESTS]
)
def test_manifest_mcp_servers_validate(
    manifest_path: Path, core_available: bool
) -> None:
    data = yaml.safe_load(manifest_path.read_text())
    servers = data.get("mcp_servers")
    if not servers:
        pytest.skip("no mcp_servers declared")
    if not core_available:
        pytest.skip("chief core checkout not found — cannot run the real loader")
    from chief.mcp_manifest import validate_mcp_servers

    problems = validate_mcp_servers(manifest_path, servers)
    assert problems == [], f"{manifest_path}: {problems}"


def test_skill_within_package_rejects_escape(tmp_path: Path) -> None:
    manifest = tmp_path / "pkg" / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    (tmp_path / "other" / "skills" / "x").mkdir(parents=True)
    (manifest.parent / "skills" / "own").mkdir(parents=True)

    assert _skill_within_package(manifest, "skills/own")
    assert not _skill_within_package(manifest, "../other/skills/x")
    assert not _skill_within_package(manifest, "/etc")
