"""The shape of a provider.

A *provider* is a company (Swiggy, Zepto). A *server* is one MCP endpoint that
provider exposes; Swiggy has two (restaurant food and Instamart), Zepto has one.
Tokens are minted per provider but stored per server, because that is the grain
the MCP client authenticates at.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthConfig:
    """How to obtain a token for one provider.

    `client_id` and `registration_url` are alternatives. Swiggy publishes a
    fixed public client id; Zepto issues one per client through Dynamic Client
    Registration (RFC 7591), so there is nothing to hard-code.

    `callback_path` is not cosmetic. Providers whitelist redirect URIs by exact
    path, and they do not agree on one: Swiggy accepts `/oauth/callback`, Zepto
    whitelists `http://127.0.0.1/callback`. RFC 8252 §7.3 says the loopback
    *port* must be ignored when matching, but the path must match, so getting
    this wrong fails the authorization request outright.
    """

    authorize_url: str
    token_url: str
    scope: str
    callback_path: str
    client_id: str | None = None
    registration_url: str | None = None
    #: RFC 8707 resource indicator. When set it is sent with both the
    #: authorization and the token request, binding the token to one MCP server.
    #: Zepto enforces this - a token minted without it authenticates fine and
    #: then fails every tool call with "The token is not intended for this
    #: resource", which surfaces as an endless re-authorization loop.
    resource: str | None = None

    def __post_init__(self) -> None:
        if not self.client_id and not self.registration_url:
            raise ValueError("provider needs either a client_id or a registration_url")


@dataclass(frozen=True)
class Server:
    """One MCP endpoint."""

    key: str        # what the user types: "food", "instamart", "zepto"
    provider: str   # provider name this belongs to
    url: str
    label: str


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    oauth: OAuthConfig
    servers: tuple[Server, ...]
