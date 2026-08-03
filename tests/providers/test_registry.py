"""The provider registry, and the invariants that keep providers apart."""

from __future__ import annotations

import pytest

from food_cli import providers
from food_cli.providers.base import OAuthConfig


def test_every_server_resolves_to_its_provider():
    assert providers.provider_for("food").name == "swiggy"
    assert providers.provider_for("instamart").name == "swiggy"
    assert providers.provider_for("zepto").name == "zepto"


def test_server_urls_are_https_and_distinct():
    urls = list(providers.SERVERS.values())
    assert all(u.startswith("https://") for u in urls)
    assert len(set(urls)) == len(urls)


def test_unknown_server_names_the_known_ones():
    with pytest.raises(providers.UnknownServerError) as e:
        providers.server("dineout")
    assert "food" in str(e.value) and "zepto" in str(e.value)


def test_a_token_never_crosses_providers():
    """The whole point of sibling_servers.

    One Swiggy sign-in should authorise Instamart too, because it is the same
    token. Writing it into Zepto's row would be silent corruption.
    """
    assert set(providers.sibling_servers("food")) == {"food", "instamart"}
    assert set(providers.sibling_servers("instamart")) == {"food", "instamart"}
    assert providers.sibling_servers("zepto") == ["zepto"]


def test_providers_disagree_about_the_callback_path():
    """If these ever converge the per-provider path can go - until then it must
    stay, because each provider whitelists redirect URIs by exact path."""
    assert providers.provider_for("food").oauth.callback_path == "/oauth/callback"
    assert providers.provider_for("zepto").oauth.callback_path == "/callback"


def test_zepto_binds_tokens_to_a_resource():
    """RFC 8707. Without it Zepto issues a token that authenticates and then
    fails every tool call with 'not intended for this resource'."""
    assert providers.provider_for("zepto").oauth.resource == "https://mcp.zepto.co.in"


def test_swiggy_has_a_static_client_id_zepto_registers():
    swiggy = providers.provider_for("food").oauth
    zepto = providers.provider_for("zepto").oauth
    assert swiggy.client_id and not swiggy.registration_url
    assert zepto.registration_url and not zepto.client_id


def test_a_provider_needs_some_way_to_identify_itself():
    with pytest.raises(ValueError, match="client_id or a registration_url"):
        OAuthConfig(
            authorize_url="https://a/authorize",
            token_url="https://a/token",
            scope="s",
            callback_path="/cb",
        )
