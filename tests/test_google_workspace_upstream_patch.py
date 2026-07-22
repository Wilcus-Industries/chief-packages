"""The pinned + patched upstream Google Workspace MCP server (#7).

Central mechanism: the auth-result-header patch makes the upstream server surface
the message's ``Authentication-Results`` header on its Gmail read output — the
exact input the sender-trust catalog (#4) verifies. These tests prove that
against the REAL public upstream, offline (no live Google): they clone the
pinned commit, apply the patch through the fail-loud installer, and then run the
patched header-building functions on a fixture Gmail payload.

The upstream module (``gmail.gmail_tools``) imports fastmcp / googleapiclient /
pydantic, none of which are installed in this docs-repo's toolchain, so we do not
import it. Instead we lift the two *pure, stdlib-only* header functions
(``_extract_headers`` + ``_format_message_header_lines``) and the
``GMAIL_METADATA_HEADERS`` constant out of the (patched or pristine) source with
``ast``, write them to a temp module, and import it — running the real upstream
bytes without the heavy deps. This is the patched central mechanism, run for
real, not mocked.

Clone gating mirrors conftest's core-checkout pattern: if the upstream cannot be
cloned (no network / no git) the clone-based tests skip *loudly*, unless
``GW_UPSTREAM_REQUIRED`` is truthy, in which case they fail rather than skip.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import repo_root

PACKAGE = repo_root() / "google-workspace"
PATCHES_DIR = PACKAGE / "patches"
PIN_FILE = PATCHES_DIR / "upstream.pin"
APPLY_SH = PACKAGE / "scripts" / "apply_upstream_patch.sh"
MANIFEST = PACKAGE / "manifest.yaml"

# The pinned server directory the manifest launch command must point at.
SERVER_DIR = "data/google-workspace/server"

# The tool-surface restriction flags that pinning must never drop (#6/#7 gate
# posture): the launch command must carry this exact subsequence.
REQUIRED_FLAGS = ["--tool-tier", "core", "--tools", "gmail", "drive", "calendar"]

_lifted_counter = itertools.count()


# --------------------------------------------------------------------------- #
# Pin file (offline — always runs)                                            #
# --------------------------------------------------------------------------- #


def _parse_pin() -> dict[str, str]:
    pin: dict[str, str] = {}
    for raw in PIN_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        pin[key.strip()] = val.strip()
    return pin


def test_pin_records_a_tag_and_commit_not_a_branch() -> None:
    """The pin is a concrete release tag + full commit SHA, never a branch."""
    pin = _parse_pin()
    assert pin["UPSTREAM_REPO"].startswith("https://")
    assert "taylorwilsdon/google_workspace_mcp" in pin["UPSTREAM_REPO"]

    tag = pin["UPSTREAM_TAG"]
    assert tag.startswith("v") and tag[1].isdigit(), f"tag must be a release: {tag}"
    assert tag not in {"main", "master", "HEAD"}, "pin must not be a moving branch"

    commit = pin["UPSTREAM_COMMIT"]
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit), (
        f"UPSTREAM_COMMIT must be a full 40-hex SHA, got {commit!r}"
    )


def test_patch_is_checked_into_the_package() -> None:
    """A patch file exists and targets the Authentication-Results header."""
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    assert patches, "no patch checked in under google-workspace/patches/"
    combined = "\n".join(p.read_text() for p in patches)
    assert "Authentication-Results" in combined
    # It touches the Gmail read path the catalog reads from.
    assert "GMAIL_METADATA_HEADERS" in combined or "gmail_helpers.py" in combined


def test_manifest_launch_pins_server_and_keeps_restriction_flags() -> None:
    """The launch command runs the pinned server dir and keeps the tool-tier /
    service restriction flags (the #6 gate posture the pin must not drop)."""
    import yaml

    data = yaml.safe_load(MANIFEST.read_text())
    command = data["mcp_servers"]["google-workspace"]["command"]
    assert isinstance(command, list) and all(isinstance(p, str) for p in command)

    # Launch targets the pinned + patched checkout, not a bare PyPI `uvx`.
    assert any(SERVER_DIR in part for part in command), (
        f"launch command must reference the pinned server dir {SERVER_DIR!r}: "
        f"{command}"
    )

    # The restriction flags appear in order as a contiguous subsequence.
    assert _contains_subsequence(command, REQUIRED_FLAGS), (
        f"launch command dropped the restriction flags {REQUIRED_FLAGS}: {command}"
    )


def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# --------------------------------------------------------------------------- #
# Clone-based tests (real upstream; skip loud without network)                #
# --------------------------------------------------------------------------- #


def _truthy(val: str | None) -> bool:
    return val is not None and val.strip().lower() not in {"", "0", "false", "no"}


@pytest.fixture(scope="session")
def pristine_upstream(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A pristine clone of the pinned upstream commit, cloned once per session.

    Skips loudly when the clone cannot be made (no network / no git), unless
    ``GW_UPSTREAM_REQUIRED`` is set — then the offline env is an error, not a
    silent skip (mirrors conftest's ``CHIEF_CORE_REQUIRED``)."""
    pin = _parse_pin()
    dst = tmp_path_factory.mktemp("gw-upstream") / "server"
    clone = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            pin["UPSTREAM_TAG"],
            pin["UPSTREAM_REPO"],
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if clone.returncode != 0:
        reason = (
            "could not clone the pinned upstream "
            f"{pin['UPSTREAM_REPO']}@{pin['UPSTREAM_TAG']} "
            f"(git rc={clone.returncode}): {clone.stderr.strip()}"
        )
        if _truthy(os.environ.get("GW_UPSTREAM_REQUIRED")):
            pytest.fail(f"GW_UPSTREAM_REQUIRED set but {reason}")
        pytest.skip(reason)
    return dst


def test_pin_commit_matches_the_cloned_tag(pristine_upstream: Path) -> None:
    """The recorded commit is really what the pinned tag resolves to."""
    pin = _parse_pin()
    head = subprocess.run(
        ["git", "-C", str(pristine_upstream), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == pin["UPSTREAM_COMMIT"], (
        f"tag {pin['UPSTREAM_TAG']} resolved to {head}, "
        f"pinned {pin['UPSTREAM_COMMIT']}"
    )


def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def _apply_patch(clone: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(APPLY_SH), str(clone)], capture_output=True, text=True
    )


def _segment(
    source: str, *, want_func: str | None = None, want_assign: str | None = None
) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if (
            want_func is not None
            and isinstance(node, ast.FunctionDef)
            and node.name == want_func
        ) or (
            want_assign is not None
            and isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == want_assign for t in node.targets
            )
        ):
            seg = ast.get_source_segment(source, node)
            assert seg is not None
            return seg
    raise AssertionError(f"could not find {want_func or want_assign} in source")


def _lift_header_funcs(server: Path, workdir: Path) -> ModuleType:
    """Import the real (patched or pristine) pure header functions as a module.

    Lifts ``GMAIL_METADATA_HEADERS`` from gmail_helpers.py and ``_extract_headers``
    + ``_format_message_header_lines`` from gmail_tools.py by source segment into a
    standalone temp module, so the actual upstream bytes run without importing the
    heavy ``gmail.gmail_tools`` (fastmcp / googleapiclient / pydantic).
    """
    helpers_src = (server / "gmail" / "gmail_helpers.py").read_text()
    tools_src = (server / "gmail" / "gmail_tools.py").read_text()

    pieces = [
        "from typing import Any, Dict, List, Optional",
        _segment(helpers_src, want_assign="GMAIL_METADATA_HEADERS"),
        _segment(tools_src, want_func="_extract_headers"),
        _segment(tools_src, want_func="_format_message_header_lines"),
    ]
    name = f"_lifted_gmail_{next(_lifted_counter)}"
    mod_path = workdir / f"{name}.py"
    mod_path.write_text("\n\n".join(pieces) + "\n")

    spec = importlib.util.spec_from_file_location(name, mod_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_payload() -> dict[str, Any]:
    """A Gmail API metadata payload for a verified message from Alice.

    The Authentication-Results value is the single-line form the Gmail API
    returns (unfolded), added at the mx.google.com boundary the catalog pins on.
    """
    return {
        "headers": [
            {"name": "From", "value": "Alice Example <alice@example.com>"},
            {"name": "To", "value": "owner@chief.test"},
            {"name": "Subject", "value": "Quarterly numbers"},
            {"name": "Date", "value": "Mon, 21 Jul 2026 09:00:00 -0700"},
            {"name": "Message-ID", "value": "<known-1@example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; "
                    "spf=pass smtp.mailfrom=alice@example.com; "
                    "dkim=pass header.i=@example.com header.s=sel; "
                    "dmarc=pass header.from=example.com"
                ),
            },
        ]
    }


def _read_output(server: Path, workdir: Path) -> str:
    mod = _lift_header_funcs(server, workdir)
    headers = mod._extract_headers(_fixture_payload(), mod.GMAIL_METADATA_HEADERS)
    lines = mod._format_message_header_lines(headers)
    return "\n".join(lines)


def test_patched_server_emits_auth_results_the_catalog_reads(
    pristine_upstream: Path, tmp_path: Path
) -> None:
    """Central mechanism: after the patch, the read output carries the
    Authentication-Results header AND the sender-trust parser resolves the
    verified sender from that output."""
    from sender_trust import verified_sender

    patched = _copy(pristine_upstream, tmp_path / "patched")
    result = _apply_patch(patched)
    assert result.returncode == 0, f"patch failed to apply: {result.stderr}"

    output = _read_output(patched, tmp_path)
    assert "Authentication-Results: mx.google.com;" in output

    # Alignment: feed the emitted output straight into #4's parser.
    assert verified_sender(output + "\n\nbody") == "alice@example.com"


def test_unpatched_server_omits_the_header(
    pristine_upstream: Path, tmp_path: Path
) -> None:
    """Negative control: without the patch the header set omits
    Authentication-Results, so the catalog cannot verify (returns None) — the
    silent-degradation state the patch exists to prevent."""
    from sender_trust import verified_sender

    output = _read_output(pristine_upstream, tmp_path)
    assert "Authentication-Results:" not in output
    assert verified_sender(output + "\n\nbody") is None


def test_mangled_target_aborts_loud(pristine_upstream: Path, tmp_path: Path) -> None:
    """If the patch does not apply (target mangled / pin bumped without a re-gen),
    the installer fails LOUD and non-zero — never a silent skip."""
    mangled = _copy(pristine_upstream, tmp_path / "mangled")
    (mangled / "gmail" / "gmail_helpers.py").write_text("# mangled — no anchor\n")

    result = _apply_patch(mangled)
    assert result.returncode != 0, "mangled target must abort with non-zero exit"
    combined = result.stdout + result.stderr
    assert "FATAL" in combined and "does NOT apply" in combined


def test_apply_is_idempotent(pristine_upstream: Path, tmp_path: Path) -> None:
    """Re-running the applier over an already-patched tree is a no-op success,
    not a double-apply failure."""
    tree = _copy(pristine_upstream, tmp_path / "idem")
    first = _apply_patch(tree)
    assert first.returncode == 0, first.stderr
    second = _apply_patch(tree)
    assert second.returncode == 0, second.stderr
    assert "already applied" in (second.stdout + second.stderr)
