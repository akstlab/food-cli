"""Zepto: quick-commerce groceries and household goods.

Everything here was read from Zepto's live discovery documents rather than
guessed:

    GET https://mcp.zepto.co.in/.well-known/oauth-protected-resource
        -> authorization_servers: ["https://auth.zepto.co.in"]
           scopes_supported:      ["tools:read", "tools:write"]

    GET https://auth.zepto.co.in/.well-known/oauth-authorization-server
        -> authorize / token / register endpoints, S256 PKCE,
           token_endpoint_auth_method: "none" (public client)

Two consequences worth knowing:

* There is no published client id. Zepto exposes a registration endpoint, so
  the CLI registers itself once and caches the issued id.
* Zepto whitelists loopback redirects at `/callback`, not at Swiggy's
  `/oauth/callback`. The path is part of the match, so it is set per provider.

Zepto does not publish its tool names, so this CLI discovers them at runtime
rather than hard-coding a surface that may not exist. See `providers/roles.py`.
"""

from __future__ import annotations

from .base import OAuthConfig, Provider, Server

PROVIDER = Provider(
    name="zepto",
    label="Zepto",
    oauth=OAuthConfig(
        authorize_url="https://auth.zepto.co.in/authorize",
        token_url="https://auth.zepto.co.in/token",
        registration_url="https://auth.zepto.co.in/register",
        scope="tools:read tools:write",
        callback_path="/callback",
        # Advertised by the protected-resource document above, and enforced.
        resource="https://mcp.zepto.co.in",
    ),
    servers=(
        Server("zepto", "zepto", "https://mcp.zepto.co.in/mcp", "Zepto quick-commerce"),
    ),
)
