"""Work out the cheapest way to cross a coupon threshold.

Swiggy says things like "Add ₹149 more to get a discount upto ₹80". The useful
question is not *whether* to top up but *what to add* - and the naive answer
("add the cheapest item repeatedly") produces daft baskets like eleven sauce
sachets.

This is a bounded knapsack: pick items (with repetition, capped) whose total is
at least the shortfall, minimising overshoot first, then item count. On top of
that we bias toward filler that a person would actually want - drinks, sides,
desserts, bestsellers - and away from ordering six identical condiments.

Everything here is arithmetic on the live menu; nothing is invented.
"""

from __future__ import annotations

import re

# Things that make sense as a top-up: small, self-contained, nice to have.
FILLER_HINTS = re.compile(
    r"\b(coke|pepsi|sprite|thums|drink|juice|lemonade|chaas|buttermilk|lassi|"
    r"water|soda|beverage|tea|coffee|"
    r"fries|nuggets|popcorn|wedges|sauce|dip|mayo|ketchup|papad|salad|raita|"
    r"curd|sweet|dessert|gulab|jamun|brownie|ice\s*cream|kulfi|cookie|"
    r"roti|naan|paratha|chapathi|rice|extra)\b",
    re.I,
)
# Rarely sensible to order several of.
AVOID_MULTIPLES = re.compile(r"\b(sauce|dip|mayo|ketchup|papad|cutlery|spoon)\b", re.I)

NON_VEG = re.compile(
    r"\b(chicken|mutton|lamb|beef|pork|fish|prawn|shrimp|crab|egg|keema|kheema|"
    r"bacon|ham|seafood|meat|non[- ]?veg|"
    r"salmon|tuna|pomfret|basa|surmai|squid|octopus|lobster|oyster|clam|mussel|"
    r"anchovy|turkey|duck|venison|sausage|salami|pepperoni|mince|liver|wings)\b",
    re.I,
)
VEG_EXCEPTION = re.compile(r"\b(veg|paneer|soya|mock|jackfruit|mushroom|gobi)\b", re.I)


def _is_non_veg(name: str) -> bool:
    """Name-based fallback. Only used when the API gives us no classification."""
    if VEG_EXCEPTION.search(name or ""):
        return False
    return bool(NON_VEG.search(name or ""))


def is_veg(item: dict) -> bool:
    """Is this item vegetarian?

    Swiggy classifies items itself - dish search renders `| Veg |` / `| Non-veg |`,
    menus render `| Veg, ...`, and Instamart sends `vegClassifier`. That flag is
    authoritative and must win: guessing from the name is only a fallback for
    items where Swiggy said nothing, and getting it wrong means serving meat to
    a vegetarian.
    """
    flag = item.get("veg")
    if flag is not None:
        return bool(flag)
    classifier = (item.get("vegClassifier") or "").upper()
    if classifier:
        return "NON_VEG" not in classifier
    return not _is_non_veg(item.get("name", ""))


def _max_copies(name: str) -> int:
    """How many of this item it is reasonable to add."""
    if AVOID_MULTIPLES.search(name or ""):
        return 2
    return 4


def _desirability(item: dict) -> float:
    """Higher is better filler. Used only to break ties between equal-cost picks."""
    score = 0.0
    if FILLER_HINTS.search(item.get("name", "")):
        score += 2.0
    if item.get("bestseller"):
        score += 1.0
    # Prefer mid-cheap items over near-zero ones; a ₹1 sachet is technically
    # optimal and practically silly.
    price = item.get("price") or 0
    if price >= 40:
        score += 0.5
    return score


def plan(
    items: list[dict],
    shortfall: float,
    veg_only: bool = False,
    max_total_items: int = 4,
    max_overshoot: float = 120.0,
) -> dict:
    """Cheapest basket of add-ons reaching at least `shortfall`.

    Returns {"found": bool, "items": [...], "added_cost": float, "overshoot": float}.
    """
    if shortfall <= 0:
        return {"found": True, "items": [], "added_cost": 0.0, "overshoot": 0.0}

    pool = [i for i in items if (i.get("price") or 0) > 0 and (not veg_only or is_veg(i))]
    if not pool:
        return {"found": False, "reason": "no candidate items on the menu"}

    need = int(round(shortfall))
    cap = need + int(round(max_overshoot))

    # dp[v] = (item_count, -desirability, choice_index, prev_v) reaching exactly v.
    INF = (10**9, 0.0, -1, -1)
    dp: list[tuple] = [INF] * (cap + 1)
    dp[0] = (0, 0.0, -1, -1)

    # Bounded repetition: expand each item into its allowed copies.
    choices: list[tuple[int, dict]] = []
    for idx, it in enumerate(pool):
        p = int(round(it["price"]))
        if p <= 0 or p > cap:
            continue
        choices.append((p, it))

    for v in range(1, cap + 1):
        best = INF
        for ci, (p, it) in enumerate(choices):
            if p > v:
                continue
            prev = dp[v - p]
            if prev[0] >= 10**9:
                continue
            # enforce per-item copy limit by walking the chain
            copies = 1
            back_v, back_ci = v - p, prev[2]
            while back_ci == ci and back_v >= 0 and dp[back_v][2] == ci:
                copies += 1
                back_v = dp[back_v][3]
                if back_v < 0:
                    break
            if copies > _max_copies(it.get("name", "")):
                continue

            cand = (prev[0] + 1, prev[1] - _desirability(it), ci, v - p)
            if cand[0] <= max_total_items and cand < best:
                best = cand
        dp[v] = best

    # Smallest reachable total at or above the shortfall.
    target = None
    for v in range(need, cap + 1):
        if dp[v][0] < 10**9:
            target = v
            break
    if target is None:
        return {
            "found": False,
            "reason": f"no combination within +₹{max_overshoot:.0f} and "
                      f"{max_total_items} items reaches ₹{shortfall:.0f}",
        }

    picked: list[dict] = []
    v = target
    while v > 0:
        _, _, ci, prev_v = dp[v]
        if ci < 0:
            break
        picked.append(choices[ci][1])
        v = prev_v

    counts: dict[str, dict] = {}
    for it in picked:
        row = counts.setdefault(it["item_id"], {**it, "quantity": 0})
        row["quantity"] += 1

    rows = [
        {
            "item_id": k,
            "name": v_["name"],
            "price": v_["price"],
            "quantity": v_["quantity"],
            "line_total": round(v_["price"] * v_["quantity"], 2),
        }
        for k, v_ in counts.items()
    ]
    added = round(sum(r["line_total"] for r in rows), 2)
    return {
        "found": True,
        "items": sorted(rows, key=lambda r: -r["line_total"]),
        "added_cost": added,
        "overshoot": round(added - shortfall, 2),
    }
