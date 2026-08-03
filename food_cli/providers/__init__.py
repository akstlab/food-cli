"""Provider registry.

Look servers and OAuth settings up by key here rather than importing a specific
provider module, so adding one is a single edit to `_PROVIDERS`.
"""

from __future__ import annotations

from .base import OAuthConfig, Provider, Server
from .swiggy import PROVIDER as SWIGGY
from .zepto import PROVIDER as ZEPTO

_PROVIDERS: tuple[Provider, ...] = (SWIGGY, ZEPTO)

PROVIDERS: dict[str, Provider] = {p.name: p for p in _PROVIDERS}
SERVERS_BY_KEY: dict[str, Server] = {s.key: s for p in _PROVIDERS for s in p.servers}

#: Server key -> URL. Kept as a plain mapping because a lot of code just wants
#: "what are the servers" without caring which company runs them.
SERVERS: dict[str, str] = {k: s.url for k, s in SERVERS_BY_KEY.items()}

__all__ = [
    "OAuthConfig", "Provider", "Server", "PROVIDERS", "SERVERS", "SERVERS_BY_KEY",
    "server", "server_url", "provider_for", "sibling_servers", "resolve_scope",
]


class UnknownServerError(KeyError):
    """Raised with a usable message instead of a bare KeyError."""


def server(key: str) -> Server:
    try:
        return SERVERS_BY_KEY[key]
    except KeyError:
        known = ", ".join(sorted(SERVERS_BY_KEY))
        raise UnknownServerError(f"unknown server {key!r} - known servers: {known}") from None


def server_url(key: str) -> str:
    return server(key).url


def provider_for(key: str) -> Provider:
    """The provider that owns a server key."""
    return PROVIDERS[server(key).provider]


def sibling_servers(key: str) -> list[str]:
    """Every server key sharing a token with this one.

    Swiggy mints one token that works for both food and Instamart, so signing in
    once should authorize both. Zepto's token must never be written to a Swiggy
    row, which is exactly what a naive "apply to all servers" would do.
    """
    return [s.key for s in provider_for(key).servers]


def resolve_scope(key: str) -> str:
    return provider_for(key).oauth.scope
