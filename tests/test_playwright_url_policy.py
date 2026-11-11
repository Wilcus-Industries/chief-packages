"""The URL policy every URL chief's browser is sent to passes: http(s) only,
never this machine. ``browser_navigate`` is an auto-approved read, so a URL
that runs script (``javascript:``) or reaches the CDP port would bypass the
approval card on ``browser_evaluate`` or take over the shared browser.
"""

from __future__ import annotations

import pytest
from url_policy import effective_url, refusal


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1#frag",
        "https://accounts.google.com/signin",
        "https://例え.jp/",
        "https://xn--r8jz45g.jp/",
        "https://8.8.8.8/",
        "https://[2001:db8::1]/",
        "HTTPS://Example.COM",
        "https://example.com:8443/",
    ],
)
def test_ordinary_web_urls_pass(url: str) -> None:
    assert refusal(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "blob:https://example.com/uuid",
        "about:blank",
        "chrome://restart",
        "chrome://quit",
        "chrome://crash",
        "file:///etc/passwd",
        "view-source:https://example.com",
        "ftp://example.com",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    assert refusal(url) == "error: url must be http(s)"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9322/json/close/ABC",
        "http://127.0.0.1:8131/s/token",
        "http://127.9.9.9/",
        "http://localhost:9322/json",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://localhost./",
        "http://127.0.0.1./",
        "http://0.0.0.0:9322/",
        "http://0/",
        "http://0.1.2.3/",
        "http://127.1:9322/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://[::1]:9322/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://%31%32%37.0.0.1:9322/",
        "http://１２７.０.０.１:9322/",
        "http://127。0。0。1/",
    ],
)
def test_this_machine_is_refused(url: str) -> None:
    assert refusal(url) == "error: url must not point at this machine (loopback)"


@pytest.mark.parametrize(
    "url",
    [
        # urlsplit reads host example.com; Chrome reads 127.0.0.1 ('\' is '/').
        "http://127.0.0.1\\@example.com/",
        # Chrome drops tabs/newlines anywhere in a URL.
        "http://127.0.0.1\t:9322/",
        "http://exa\nmple.com/",
        " https://example.com",
        "https://example.com/ x",
        "https://example.com\x00/",
    ],
)
def test_parser_confusion_characters_are_refused(url: str) -> None:
    assert refusal(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/",
        "https://example.com@127.0.0.1/",
        "https://127.0.0.1@example.com/",
    ],
)
def test_userinfo_is_refused(url: str) -> None:
    assert refusal(url) == "error: url must not carry a username or password"


@pytest.mark.parametrize(
    "url", ["https://", "http:///path", "https://:443/", "https://ex%2fample.com/"]
)
def test_missing_or_odd_hosts_are_refused(url: str) -> None:
    assert refusal(url) is not None


def test_bad_port_is_refused() -> None:
    assert refusal("https://example.com:99999/") == "error: url is malformed"


def test_effective_url_mirrors_the_pinned_server() -> None:
    # A scheme-less URL is what the pinned server prefixes, so check that one.
    assert effective_url("example.com/x") == "https://example.com/x"
    assert effective_url("localhost:9322/json") == "http://localhost:9322/json"
    assert effective_url("127.0.0.1:9322") == "http://127.0.0.1:9322"
    assert effective_url("javascript:alert(1)") == "javascript:alert(1)"
    assert effective_url("https://example.com") == "https://example.com"
    assert refusal(effective_url("localhost:9322/json")) is not None
    assert refusal(effective_url("example.com")) is None
