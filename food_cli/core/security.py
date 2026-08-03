"""Security helpers.

The CLI consumes URLs that arrive inside MCP tool responses — image links, and
the payment "bridge" page. Those are data from a remote server, not values this
program chose, so anything that acts on them (fetching, opening, writing to
disk) has to be constrained.

Three concrete risks this module addresses:

1. **SSRF.** `httpx.get(url_from_response)` would happily fetch
   `http://169.254.169.254/…` (cloud metadata) or a service on localhost, and
   we then write the body to disk. Fetches are restricted to public hosts over
   https, with a size cap.

2. **Arbitrary URI handling.** `subprocess.run(["open", value])` on macOS hands
   the value to the default handler for *any* scheme — `file://`, or a custom
   scheme registered by some installed app. Only https URLs and files we wrote
   ourselves are ever opened.

3. **Local disclosure.** The store holds OAuth tokens and saved addresses, and
   generated QR codes encode a live payment intent. Directories are created
   0700 and files 0600, rather than inheriting a world-readable umask.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Max bytes we will pull from a remote URL found in a tool response.
MAX_FETCH_BYTES = 10 * 1024 * 1024


class UnsafeURLError(ValueError):
    """Raised when a URL from a tool response fails validation."""


def _resolved_ips(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        # Catch OSError, not just socket.gaierror. Sandboxes and containers can
        # fail resolution in other ways (seen: EBUSY "Device or resource busy"),
        # and an uncaught error here would take down the caller rather than
        # simply refusing the URL. Failing closed is the correct posture.
        raise UnsafeURLError(f"cannot resolve host {host!r}: {e}") from e
    out = []
    for info in infos:
        addr = info[4][0]
        try:
            out.append(ipaddress.ip_address(addr.split("%")[0]))
        except ValueError:
            continue
    return out


def check_url(url: str, *, allow_http: bool = False) -> str:
    """Validate a URL that came from a tool response before we act on it.

    Rejects non-https schemes and any host that resolves to a private,
    loopback, link-local, or otherwise reserved address.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("empty URL")

    parsed = urlparse(url)
    allowed = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme not in allowed:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} not allowed (want https)")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no host")

    for ip in _resolved_ips(parsed.hostname):
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise UnsafeURLError(
                f"host {parsed.hostname!r} resolves to non-public address {ip}"
            )
    return url


def safe_get(url: str, *, timeout: float = 20.0, max_bytes: int = MAX_FETCH_BYTES,
             allow_http: bool = False) -> bytes | None:
    """Fetch a validated URL, capped in size. Returns None on any failure.

    Redirects are followed manually so that each hop is re-validated — a
    permitted host must not be able to bounce us onto a private address.
    """
    try:
        current = check_url(url, allow_http=allow_http)
    except UnsafeURLError:
        return None

    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as cl:
            for _ in range(5):
                r = cl.get(current)
                if r.status_code in (301, 302, 303, 307, 308):
                    nxt = r.headers.get("location")
                    if not nxt:
                        return None
                    current = check_url(str(httpx.URL(current).join(nxt)),
                                        allow_http=allow_http)
                    continue
                if r.status_code != 200:
                    return None
                content = r.content
                return content[:max_bytes] if content else None
    except (httpx.HTTPError, UnsafeURLError, ValueError, OSError):
        # OSError covers socket-level failures in restricted environments; a
        # fetch that cannot complete is simply "no content", never a crash.
        return None
    return None


def is_openable(target: str, allowed_roots: list[Path]) -> bool:
    """May we hand this to the OS 'open' handler?

    Only https URLs, or a real file inside a directory this program owns.
    Anything else (file://, custom app schemes, paths elsewhere on disk) is
    refused, because the value may have come from a remote response.
    """
    if target.startswith("https://"):
        try:
            check_url(target)
            return True
        except UnsafeURLError:
            return False

    try:
        path = Path(target).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not path.is_file():
        return False
    return any(
        path.is_relative_to(root.resolve()) for root in allowed_roots if root.exists()
    )


def secure_dir(path: Path) -> Path:
    """Create a directory only the owner can read. Tokens and payment QRs live here."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def secure_write_bytes(path: Path, data: bytes) -> Path:
    """Write owner-only, refusing to follow a pre-existing symlink.

    Matters for shared directories such as /tmp, where another local user could
    plant a symlink at a predictable filename.
    """
    if path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
