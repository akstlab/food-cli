"""Capability -> tool-name resolution.

Published surfaces are asserted directly. The scoring path matters for any
server that does not publish its tool names: a wrong guess there would call a
tool that spends money.
"""

from __future__ import annotations

import pytest

from food_cli.providers import roles

pytestmark = pytest.mark.usefixtures("fresh_db")


def _tool(name, description=""):
    return {"name": name, "description": description, "input_schema": {}}


def test_published_surfaces_are_stated_not_guessed():
    for server in ("food", "instamart", "zepto"):
        mapping = roles.discover(server)
        assert mapping["search"]
        assert mapping["order_place"]
        assert set(mapping) == set(roles.ROLES)


def test_zepto_places_a_cod_order_by_default():
    """create_order is the COD tool; the paid variants are chosen explicitly."""
    assert roles.KNOWN["zepto"]["order_place"] == "create_order"
    assert roles.KNOWN["zepto"]["cart_update"] == "update_cart"


def test_scoring_prefers_the_name_over_the_description():
    tools = [
        _tool("list_order_history", "past orders"),
        _tool("unrelated", "this description mentions order history at length"),
    ]
    assert roles.map_tools(tools)["order_history"] == "list_order_history"


def test_clear_and_update_do_not_collapse_onto_one_tool():
    tools = [_tool("update_cart", "add items"), _tool("clear_cart", "empty the cart")]
    m = roles.map_tools(tools)
    assert m["cart_update"] == "update_cart"
    assert m["cart_clear"] == "clear_cart"


def test_reading_the_cart_is_not_writing_to_it():
    tools = [_tool("view_cart", "show cart"), _tool("update_cart", "add to cart")]
    m = roles.map_tools(tools)
    assert m["cart_get"] == "view_cart"
    assert m["cart_update"] == "update_cart"


def test_a_bare_word_match_is_not_enough():
    """`search` alone should not be claimed by something that merely says
    'order'. An unresolved role is safer than a wrong one."""
    assert roles.map_tools([_tool("ping", "checks the order of things")])["order_place"] is None


def test_unmatched_roles_resolve_to_none():
    m = roles.map_tools([_tool("do_nothing", "nothing at all")])
    assert all(v is None for v in m.values())


def test_resolve_refuses_to_guess_and_says_what_to_run(monkeypatch):
    monkeypatch.setattr(roles, "discover", lambda s, refresh=False: {"search": None})
    with pytest.raises(LookupError) as e:
        roles.resolve("someserver", "search")
    assert "food mcp call" in str(e.value)


def test_unknown_role_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown role"):
        roles.resolve("zepto", "teleport")


def test_discovery_is_cached(monkeypatch):
    calls = []

    def fake_list(server, refresh=False):
        calls.append(server)
        return [_tool("search_things", "search the catalog for a product")]

    monkeypatch.setattr(roles, "list_cached_tools", fake_list)
    first = roles.discover("newserver")
    second = roles.discover("newserver")
    assert first == second
    assert len(calls) == 1, "second discover should read the cache"


def test_param_named_matches_however_the_vendor_spells_it():
    schema = {"properties": {"searchTerm": {"type": "string"}}}
    assert roles.param_named(schema, ("query", "term")) == "searchTerm"
    assert roles.param_named({"properties": {}}, ("query",)) is None
