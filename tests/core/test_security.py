"""Security guards: SSRF, URI handling, file permissions.

Values reaching these helpers can originate in a remote MCP response, so the
negative cases matter more than the positive ones.
"""

from __future__ import annotations

import os

import httpx
import pytest

from food_cli.core import security


# ------------------------------------------------------------- URL checking

@pytest.mark.parametrize("url", [
    "http://example.com/x",          # plain http
    "file:///etc/passwd",            # local file
    "javascript:alert(1)",           # script scheme
    "myapp://launch",                # custom app scheme
    "ftp://example.com/x",
    "",
    None,
])
def test_check_url_rejects_bad_schemes(url):
    with pytest.raises(security.UnsafeURLError):
        security.check_url(url)


@pytest.mark.parametrize("url,label", [
    ("https://127.0.0.1/x", "loopback"),
    ("https://localhost/x", "localhost"),
    ("https://169.254.169.254/latest/meta-data/", "cloud metadata"),
    ("https://192.168.0.1/x", "private LAN"),
    ("https://10.0.0.1/x", "private class A"),
    ("https://[::1]/x", "IPv6 loopback"),
])
def test_check_url_blocks_internal_targets(url, label):
    with pytest.raises(security.UnsafeURLError):
        security.check_url(url)


def test_check_url_rejects_missing_host():
    with pytest.raises(security.UnsafeURLError):
        security.check_url("https:///nohost")


def test_check_url_allows_http_when_opted_in(monkeypatch):
    monkeypatch.setattr(security, "_resolved_ips",
                        lambda h: [__import__("ipaddress").ip_address("93.184.216.34")])
    assert security.check_url("http://example.com/x", allow_http=True)


def test_check_url_allows_public_https(monkeypatch):
    monkeypatch.setattr(security, "_resolved_ips",
                        lambda h: [__import__("ipaddress").ip_address("93.184.216.34")])
    assert security.check_url("https://example.com/x")


def test_unresolvable_host_is_rejected():
    with pytest.raises(security.UnsafeURLError):
        security.check_url("https://this-host-does-not-exist.invalid/x")


# ----------------------------------------------------------------- safe_get

@pytest.mark.real_fetch
def test_safe_get_refuses_unsafe_url():
    assert security.safe_get("http://169.254.169.254/") is None
    assert security.safe_get("file:///etc/passwd") is None


@pytest.mark.real_fetch
def test_safe_get_returns_body(monkeypatch):
    monkeypatch.setattr(security, "check_url", lambda u, **k: u)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(200, content=b"hello")

    monkeypatch.setattr(security.httpx, "Client", FakeClient)
    assert security.safe_get("https://ok/x") == b"hello"


@pytest.mark.real_fetch
def test_safe_get_caps_size(monkeypatch):
    monkeypatch.setattr(security, "check_url", lambda u, **k: u)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(200, content=b"x" * 5000)

    monkeypatch.setattr(security.httpx, "Client", FakeClient)
    assert len(security.safe_get("https://ok/x", max_bytes=100)) == 100


@pytest.mark.real_fetch
def test_safe_get_revalidates_redirect_target(monkeypatch):
    """A permitted host must not be able to bounce us onto a private address."""
    seen = []

    def fake_check(u, **k):
        seen.append(u)
        if "169.254" in u:
            raise security.UnsafeURLError("private")
        return u

    monkeypatch.setattr(security, "check_url", fake_check)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    monkeypatch.setattr(security.httpx, "Client", FakeClient)
    assert security.safe_get("https://ok/x") is None
    assert any("169.254" in s for s in seen)      # the hop was checked, not followed


@pytest.mark.real_fetch
def test_safe_get_non_200(monkeypatch):
    monkeypatch.setattr(security, "check_url", lambda u, **k: u)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(404)

    monkeypatch.setattr(security.httpx, "Client", FakeClient)
    assert security.safe_get("https://ok/x") is None


@pytest.mark.real_fetch
def test_safe_get_handles_transport_error(monkeypatch):
    monkeypatch.setattr(security, "check_url", lambda u, **k: u)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(security.httpx, "Client", FakeClient)
    assert security.safe_get("https://ok/x") is None


# --------------------------------------------------------------- is_openable

def test_is_openable_rejects_foreign_paths(tmp_path):
    outsider = tmp_path / "outside.png"
    outsider.write_bytes(b"x")
    assert security.is_openable(str(outsider), [tmp_path / "allowed"]) is False


def test_is_openable_allows_our_own_file(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    f = root / "a.png"
    f.write_bytes(b"x")
    assert security.is_openable(str(f), [root]) is True


def test_is_openable_rejects_missing_file(tmp_path):
    assert security.is_openable(str(tmp_path / "nope.png"), [tmp_path]) is False


def test_is_openable_rejects_directory(tmp_path):
    assert security.is_openable(str(tmp_path), [tmp_path]) is False


def test_is_openable_rejects_dangerous_schemes(tmp_path):
    for bad in ("file:///etc/passwd", "myapp://x", "javascript:alert(1)",
                "http://example.com"):
        assert security.is_openable(bad, [tmp_path]) is False


def test_is_openable_allows_public_https(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "check_url", lambda u, **k: u)
    assert security.is_openable("https://example.com/x", [tmp_path]) is True


def test_is_openable_rejects_private_https(tmp_path):
    assert security.is_openable("https://127.0.0.1/x", [tmp_path]) is False


# ------------------------------------------------------------- file writing

def test_secure_dir_is_owner_only(tmp_path):
    d = security.secure_dir(tmp_path / "private")
    assert oct(os.stat(d).st_mode & 0o777) == "0o700"


def test_secure_write_is_owner_only(tmp_path):
    f = security.secure_write_bytes(tmp_path / "f.bin", b"secret")
    assert f.read_bytes() == b"secret"
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"


def test_secure_write_replaces_symlink(tmp_path):
    """A planted symlink in a shared dir must not be followed."""
    target = tmp_path / "victim.txt"
    target.write_text("original")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    security.secure_write_bytes(link, b"new")
    assert target.read_text() == "original"      # victim untouched
    assert link.read_bytes() == b"new"
    assert not link.is_symlink()


def test_secure_write_overwrites_plain_file(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"old-and-longer")
    security.secure_write_bytes(f, b"new")
    assert f.read_bytes() == b"new"


# --------------------------------------------- regressions from the audit

def test_callback_page_escapes_query_params():
    """Reflected XSS: the callback page must not echo raw query parameters."""
    import threading
    import urllib.parse
    from tests.conftest import loopback_get

    from food_cli.mcp import client

    cb = client._CallbackServer()
    cb.start()
    got = {}
    t = threading.Thread(target=lambda: got.update(cb.wait(timeout=10)))
    t.start()
    payload = urllib.parse.quote("<script>alert(1)</script>")
    body = loopback_get(f"{cb.redirect_uri}?error={payload}")
    t.join(timeout=10)
    assert "<script>" not in body
    assert "alert(1)" not in body


def test_media_filename_extension_is_sanitised():
    """An extension taken from a remote URL must not contain path separators."""
    from food_cli.core import media

    for url in ("https://x/a.b/c", "https://x/a...", "https://x/a.%2f%2e"):
        name = media._filename(url)
        assert "/" not in name
        assert ".." not in name


def test_media_filename_keeps_normal_extensions():
    from food_cli.core import media
    assert media._filename("https://x/a.png").endswith(".png")
    assert media._filename("https://x/a.JPEG").endswith(".jpeg")


def test_oauth_rejects_redirect_url_without_state():
    from food_cli.mcp import oauth

    oauth.build_authorize_url("food")
    with pytest.raises(ValueError, match="no .state"):
        oauth.exchange("http://127.0.0.1:21621/oauth/callback?code=ABC")


@pytest.mark.real_fetch
def test_resolution_failure_fails_closed(monkeypatch):
    """Any resolver error must refuse the URL, not escape as OSError.

    Sandboxes can fail getaddrinfo with errors other than socket.gaierror
    (observed: EBUSY). An uncaught OSError there would crash the caller.
    """
    import socket as _socket

    from tests.conftest import REAL_RESOLVED_IPS

    # Put the real resolver back: it is the function under test.
    monkeypatch.setattr(security, "_resolved_ips", REAL_RESOLVED_IPS)

    for exc in (
        _socket.gaierror("name resolution failed"),
        OSError(16, "Device or resource busy"),
        PermissionError("blocked"),
    ):
        def boom(*a, _e=exc, **k):
            raise _e

        monkeypatch.setattr(security.socket, "getaddrinfo", boom)
        with pytest.raises(security.UnsafeURLError):
            security.check_url("https://example.com/x")
        # and the fetch wrapper degrades to "no content" rather than raising
        assert security.safe_get("https://example.com/x") is None


def test_qr_and_media_survive_broken_dns(monkeypatch):
    """The order flow must not break just because DNS is unavailable."""
    from food_cli.core import media, qr

    def boom(*a, **k):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(security.socket, "getaddrinfo", boom)
    assert qr._resolve_bridge("https://example.com/deeplink-redirect?x=1") is None
    assert media.download("https://example.com/a.jpg") is None
    # a bridge URL that cannot be resolved still degrades to an image_url
    found = qr.find_qr({"bridgeUrl": "https://example.com/deeplink-redirect?x=1"})
    assert found["kind"] == "image_url"
