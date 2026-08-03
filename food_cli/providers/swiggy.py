"""Swiggy: restaurant food and Instamart groceries.

Endpoints and client id come from Swiggy's own published MCP configuration.
"""

from __future__ import annotations

from .base import OAuthConfig, Provider, Server

PROVIDER = Provider(
    name="swiggy",
    label="Swiggy",
    oauth=OAuthConfig(
        authorize_url="https://mcp.swiggy.com/auth/authorize",
        token_url="https://mcp.swiggy.com/auth/token",
        client_id="swiggy-mcp",
        # offline_access is the standard way to ask for a refresh token.
        # Swiggy's server advertises the refresh_token grant but TESTED: it does
        # not issue one to this public client. We ask anyway so refresh starts
        # working by itself if that changes - but do not promise users it will.
        scope="mcp:tools mcp:resources mcp:prompts offline_access",
        callback_path="/oauth/callback",
    ),
    servers=(
        Server("food", "swiggy", "https://mcp.swiggy.com/food", "Swiggy restaurant food"),
        Server("instamart", "swiggy", "https://mcp.swiggy.com/im", "Swiggy Instamart groceries"),
    ),
)
