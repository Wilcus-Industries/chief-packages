"""Deterministic upstream-release check (slice #9).

Central mechanism under test: reading the installed pin
(``patches/upstream.pin``), asking a GitHub *latest release* endpoint for the
newest stable tag, comparing the two, and emitting an owner notification ONLY
when a newer release exists — with **no** model call anywhere in the path.

The comparison and the notification are pure functions, unit-tested directly.
The end-to-end ``check`` runs against a local fixture HTTP server standing in for
GitHub's releases API (the same fixture-endpoint pattern the #8 OAuth tests use),
so nothing here touches the network.

The module under test lives at ``google-workspace/scripts/version_check.py``;
``conftest`` puts that dir on ``sys.path`` so it imports by bare name.
"""

from __future__ import annotations

import ast
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import version_check as vc
from conftest import repo_root
from version_check import (
    EXIT_ERROR,
    EXIT_NEWER,
    EXIT_UP_TO_DATE,
    VersionCheckError,
    VersionCheckResult,
    check,
    is_newer,
    latest_tag,
    main,
    notification,
    parse_pin,
    releases_api_url,
)

PIN_FILE = repo_root() / "google-workspace" / "patches" / "upstream.pin"
MODULE_PATH = repo_root() / "google-workspace" / "scripts" / "version_check.py"


# --------------------------------------------------------------------------- #
# No model in the path — pinned by construction                               #
# --------------------------------------------------------------------------- #


_STDLIB_IMPORTS = {
    "__future__",
    "argparse",
    "json",
    "os",
    "sys",
    "urllib",
    "dataclasses",
    "pathlib",
    "typing",
}


def test_module_imports_only_stdlib_no_model_in_path() -> None:
    """The version check must be deterministic: it may import only stdlib — no
    chief, no provider, no classifier, nothing that could put an LLM in the path.
    Pin it against the module's own import list."""
    tree = ast.parse(MODULE_PATH.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "chief" not in imported, "version check must not import chief"
    extra = imported - _STDLIB_IMPORTS
    assert not extra, f"version check imports non-stdlib modules: {sorted(extra)}"


# --------------------------------------------------------------------------- #
# Pin parsing + endpoint derivation                                           #
# --------------------------------------------------------------------------- #


def test_parse_pin_reads_the_real_checked_in_pin() -> None:
    pin = parse_pin(PIN_FILE)
    assert "taylorwilsdon/google_workspace_mcp" in pin["UPSTREAM_REPO"]
    assert pin["UPSTREAM_TAG"].startswith("v")


def test_parse_pin_missing_file_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(VersionCheckError, match="cannot read"):
        parse_pin(tmp_path / "nope.pin")


def test_parse_pin_without_repo_or_tag_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "bad.pin"
    p.write_text("# only a comment\nUPSTREAM_COMMIT=deadbeef\n")
    with pytest.raises(VersionCheckError, match="UPSTREAM_REPO"):
        parse_pin(p)


def test_releases_api_url_from_github_repo() -> None:
    url = releases_api_url("https://github.com/taylorwilsdon/google_workspace_mcp")
    assert url == (
        "https://api.github.com/repos/taylorwilsdon/google_workspace_mcp/"
        "releases/latest"
    )


def test_releases_api_url_strips_dot_git() -> None:
    url = releases_api_url("https://github.com/owner/repo.git")
    assert url.endswith("/repos/owner/repo/releases/latest")


def test_releases_api_url_rejects_non_github() -> None:
    with pytest.raises(VersionCheckError, match="github.com"):
        releases_api_url("https://gitlab.com/owner/repo")


# --------------------------------------------------------------------------- #
# Version comparison (deterministic)                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("latest", "pinned", "expected"),
    [
        ("v1.23.0", "v1.22.0", True),
        ("v2.0.0", "v1.22.0", True),
        ("v1.22.1", "v1.22.0", True),
        ("v1.22.0", "v1.22.0", False),  # equal -> not newer
        ("v1.21.0", "v1.22.0", False),  # older -> not newer
        ("1.22.0", "v1.22.0", False),  # leading-v optional, still equal
        ("v1.22", "v1.22.0", False),  # zero-pad: v1.22 == v1.22.0
        ("v1.22.0", "v1.22.0-rc.1", True),  # final > prerelease of same release
        ("v1.22.0-rc.2", "v1.22.0-rc.1", True),  # prerelease ordering
    ],
)
def test_is_newer(latest: str, pinned: str, expected: bool) -> None:
    assert is_newer(latest, pinned) is expected


def test_latest_tag_requires_tag_name() -> None:
    with pytest.raises(VersionCheckError, match="tag_name"):
        latest_tag({"name": "no tag here"})


def test_notification_only_when_newer() -> None:
    newer = VersionCheckResult(
        repo="https://github.com/o/r",
        pinned_tag="v1.22.0",
        latest_tag="v1.23.0",
        latest_url="https://github.com/o/r/releases/tag/v1.23.0",
        newer=True,
    )
    msg = notification(newer)
    assert msg is not None
    assert "v1.23.0" in msg and "v1.22.0" in msg
    assert "upstream.pin" in msg  # names the deliberate follow-up

    up_to_date = VersionCheckResult(
        repo="https://github.com/o/r",
        pinned_tag="v1.22.0",
        latest_tag="v1.22.0",
        latest_url="https://github.com/o/r",
        newer=False,
    )
    assert notification(up_to_date) is None


# --------------------------------------------------------------------------- #
# End-to-end check against a fixture releases endpoint                        #
# --------------------------------------------------------------------------- #


class _FixtureReleases(http.server.BaseHTTPRequestHandler):
    """Stand-in for GitHub's ``releases/latest``. Replies with the tag set on the
    class; a ``bad-json`` tag triggers a malformed body, ``http-404`` a 404."""

    tag: str = "v1.22.0"

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if type(self).tag == "http-404":
            self.send_response(404)
            body = b'{"message": "Not Found"}'
        elif type(self).tag == "bad-json":
            self.send_response(200)
            body = b"not json at all"
        else:
            self.send_response(200)
            body = json.dumps(
                {
                    "tag_name": type(self).tag,
                    "html_url": (
                        "https://github.com/taylorwilsdon/google_workspace_mcp/"
                        f"releases/tag/{type(self).tag}"
                    ),
                }
            ).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test noise
        return


@pytest.fixture
def releases_endpoint() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _FixtureReleases)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/releases/latest"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _pin(tmp_path: Path, tag: str = "v1.22.0") -> Path:
    p = tmp_path / "upstream.pin"
    p.write_text(
        "UPSTREAM_REPO=https://github.com/taylorwilsdon/google_workspace_mcp\n"
        f"UPSTREAM_TAG={tag}\n"
        "UPSTREAM_COMMIT=6e1d1457746777f8512f52d40fb195b2a40bad36\n"
    )
    return p


def test_check_detects_a_newer_release_and_notifies(
    releases_endpoint: str, tmp_path: Path
) -> None:
    """Acceptance: given a newer upstream release, the deterministic check
    produces a notification — no model call in the path."""
    _FixtureReleases.tag = "v1.23.0"
    result = check(_pin(tmp_path, "v1.22.0"), endpoint=releases_endpoint)
    assert result.newer is True
    assert result.latest_tag == "v1.23.0"
    assert result.pinned_tag == "v1.22.0"
    message = notification(result)
    assert message is not None and "v1.23.0" in message


def test_check_reports_up_to_date_when_equal(
    releases_endpoint: str, tmp_path: Path
) -> None:
    """The negative half: an equal latest release yields no notification."""
    _FixtureReleases.tag = "v1.22.0"
    result = check(_pin(tmp_path, "v1.22.0"), endpoint=releases_endpoint)
    assert result.newer is False
    assert notification(result) is None


def test_check_up_to_date_when_upstream_is_older(
    releases_endpoint: str, tmp_path: Path
) -> None:
    _FixtureReleases.tag = "v1.20.0"
    result = check(_pin(tmp_path, "v1.22.0"), endpoint=releases_endpoint)
    assert result.newer is False


def test_check_http_error_fails_loud(
    releases_endpoint: str, tmp_path: Path
) -> None:
    _FixtureReleases.tag = "http-404"
    with pytest.raises(VersionCheckError, match="404"):
        check(_pin(tmp_path), endpoint=releases_endpoint)


def test_check_bad_json_fails_loud(
    releases_endpoint: str, tmp_path: Path
) -> None:
    _FixtureReleases.tag = "bad-json"
    with pytest.raises(VersionCheckError, match="non-JSON"):
        check(_pin(tmp_path), endpoint=releases_endpoint)


# --------------------------------------------------------------------------- #
# CLI exit codes (so a timer can branch without parsing stdout)               #
# --------------------------------------------------------------------------- #


def test_main_exit_newer_prints_notification(
    releases_endpoint: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _FixtureReleases.tag = "v1.23.0"
    code = main(
        ["--pin", str(_pin(tmp_path)), "--releases-endpoint", releases_endpoint]
    )
    assert code == EXIT_NEWER
    out = capsys.readouterr().out
    assert "v1.23.0" in out  # the notification is on stdout for a timer to pipe


def test_main_exit_up_to_date_is_quiet_on_stdout(
    releases_endpoint: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _FixtureReleases.tag = "v1.22.0"
    code = main(
        ["--pin", str(_pin(tmp_path)), "--releases-endpoint", releases_endpoint]
    )
    assert code == EXIT_UP_TO_DATE
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # nothing to notify
    assert "up to date" in captured.err


def test_main_exit_error_on_bad_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--pin", str(tmp_path / "missing.pin")])
    assert code == EXIT_ERROR
    assert "version_check:" in capsys.readouterr().err


def test_exit_codes_are_distinct() -> None:
    assert len({EXIT_UP_TO_DATE, EXIT_ERROR, EXIT_NEWER}) == 3
    assert vc.EXIT_UP_TO_DATE == 0
