"""Chains with more than one nearby outlet.

The same dish from a farther outlet of the same chain is strictly worse: same
food, longer wait, usually a bigger delivery fee. The CLI decides this rather
than leaving two identically-named rows for the caller to pick between.
"""

from __future__ import annotations

import pytest

from food_cli.cli import app
from food_cli.commands.food import brand_of
from tests.conftest import parse_out

pytestmark = pytest.mark.usefixtures("fresh_db")

TWO_OUTLETS = (
    'Found 2 restaurants for "burger":\n'
    "1. Bun Bros, Northgate — Fast Food | 4.2★ | 45 min | ₹400 for two (ID: 7001)\n"
    "2. Bun Bros, Southgate — Fast Food | 4.1★ | 18 min | ₹400 for two (ID: 7002)\n"
)

DISH_TWO_OUTLETS = (
    'Found 2 menu items for "burger":\n'
    "1. Cheese Burger — ₹150 | Veg | 4.2★ | Bun Bros, Northgate "
    "(restaurantId: 7001) (ID: it_701)\n"
    "2. Cheese Burger — ₹150 | Veg | 4.1★ | Bun Bros, Southgate "
    "(restaurantId: 7002) (ID: it_702)\n"
)


@pytest.mark.parametrize("name,brand", [
    ("Bun Bros, Northgate", "bunbros"),
    ("Bun Bros - Southgate", "bunbros"),
    ("Bun Bros (Eastgate)", "bunbros"),
    ("Bun Bros", "bunbros"),
    ("bun  bros, X", "bunbros"),
])
def test_outlet_suffixes_fold_to_one_brand(name, brand):
    assert brand_of(name) == brand


def test_different_chains_do_not_merge():
    assert brand_of("Bun Bros, X") != brand_of("Bap Bros, X")


def test_search_marks_the_nearest_outlet(runner, mcp):
    mcp.set("food", "search_restaurants", TWO_OUTLETS)
    data = parse_out(runner.invoke(app, ["restaurant", "search", "burger"]))
    nearest = [r for r in data["restaurants"] if r.get("nearest_branch")]
    assert [r["id"] for r in nearest] == ["7002"], "18 min beats 45 min"
    assert data["brand_choices"][0]["chosen"]["id"] == "7002"
    assert data["brand_choices"][0]["rejected"][0]["id"] == "7001"


def test_search_says_why_it_matters(runner, mcp):
    mcp.set("food", "search_restaurants", TWO_OUTLETS)
    data = parse_out(runner.invoke(app, ["restaurant", "search", "burger"]))
    assert "delivery fee" in data["branch_note"]


def test_a_single_outlet_is_not_annotated(runner, mcp):
    data = parse_out(runner.invoke(app, ["restaurant", "search", "pizza"]))
    assert "brand_choices" not in data


def test_dish_keeps_only_the_nearest_outlet(runner, mcp):
    mcp.set("food", "search_menu", DISH_TWO_OUTLETS)
    # restaurant_eta reads the ETA off the restaurant listing, so both outlets
    # resolve from this one response: 7001 at 45 min, 7002 at 18 min.
    mcp.set("food", "search_restaurants", TWO_OUTLETS)
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "burger"]))
    assert [d["restaurant_id"] for d in data["dishes"]] == ["7002"]
    assert data["brand_choices"][0]["chosen"]["restaurant_id"] == "7002"
    assert "dropped 1 farther" in data["branch_note"]


def test_all_branches_keeps_both_and_still_flags_them(runner, mcp):
    mcp.set("food", "search_menu", DISH_TWO_OUTLETS)
    mcp.set("food", "search_restaurants", TWO_OUTLETS)
    data = parse_out(runner.invoke(
        app, ["restaurant", "dish", "burger", "--all-branches"]))
    assert len(data["dishes"]) == 2
    rejected = [d for d in data["dishes"] if d.get("nearest_branch") is False]
    assert [d["restaurant_id"] for d in rejected] == ["7001"]


def test_a_rejected_outlet_explains_itself(runner, mcp):
    mcp.set("food", "search_menu", DISH_TWO_OUTLETS)
    mcp.set("food", "search_restaurants", TWO_OUTLETS)
    data = parse_out(runner.invoke(
        app, ["restaurant", "dish", "burger", "--all-branches"]))
    reason = next(d["skip_reason"] for d in data["dishes"]
                  if d.get("nearest_branch") is False)
    assert "45" in reason and "18" in reason
    assert "delivery fee" in reason


def test_outlets_are_not_ranked_on_a_missing_eta(runner, mcp):
    """With nothing to compare, keep both rather than pick on no evidence."""
    mcp.set("food", "search_menu", DISH_TWO_OUTLETS)
    mcp.set("food", "search_restaurants",
            'Found 2 restaurants for "burger":\n'
            "1. Bun Bros, Northgate —  | undefined★ | ? min |  (ID: 7001)\n"
            "2. Bun Bros, Southgate —  | undefined★ | ? min |  (ID: 7002)\n")
    data = parse_out(runner.invoke(app, ["restaurant", "dish", "burger"]))
    assert len(data["dishes"]) == 2
    assert "brand_choices" not in data


def test_eta_is_only_looked_up_for_repeated_chains(runner, mcp):
    """One outlet per chain must not cost an ETA lookup each.

    The default dish fixture has three different restaurants, so nothing needs
    comparing and no restaurant listing should be fetched.
    """
    runner.invoke(app, ["restaurant", "dish", "platter"])
    assert "search_restaurants" not in mcp.tools_called()
