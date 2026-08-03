"""Coupon parsing and best-offer selection.

Swiggy's food MCP tool returns coupons as prose, one per line, e.g.

    Found 2 coupons (0 applicable):
    **Great deal you're missing out on!**
      - JUMBO [❌ NOT APPLICABLE] — Add ₹149 more to get a discount upto ₹80 (code: b515ca4e-...)
    **More offers**
      - PARTY [❌ NOT APPLICABLE] — Add ₹770 more to avail this offer (code: 3e00f5ea-...)

Two traps this parser exists to avoid:

  1. "Add ₹149 more" is a SHORTFALL, not a discount. Reading it as a saving
     ranks the worst coupon first.
  2. A coupon can be listed but flagged NOT APPLICABLE. Applying it fails, or
     worse, silently does nothing.

So: applicability is parsed explicitly, and the shortfall clause is removed
before any discount figure is extracted.
"""

from __future__ import annotations

import re

# "- JUMBO [❌ NOT APPLICABLE] — ..." / "- SAVE50 [✅ APPLICABLE] — ..."
#
# Codes are not always plain words: real ones look like FLAT85OFF-ABOVE249, so
# `-`, `.` and `+` are part of the code, not delimiters. Stopping at the first
# hyphen truncated the code and Swiggy then rejected it, silently costing the
# user the discount. The class still excludes whitespace and the em dash, so it
# cannot run on into the description.
_LINE = re.compile(r"^\s*[-*]\s*([A-Z0-9][A-Z0-9_.+-]{1,49})\s*(\[[^\]]*\])?\s*(.*)$")
# Trailing punctuation belongs to the sentence, not the code.
_CODE_TRIM = "-_.+"
_NOT_APPLICABLE = re.compile(r"not\s*applicable|❌", re.I)
_APPLICABLE = re.compile(r"applicable|✅", re.I)

# The shortfall clause - stripped out before discount parsing.
_SHORTFALL = re.compile(r"add\s*(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)\s*more", re.I)

_UUID = re.compile(r"\(code:\s*([0-9a-fA-F-]{8,})\s*\)")
# Swiggy often states only the shortfall, never the saving - but a code like
# FLAT150, or FLAT85OFF-ABOVE249, names its own discount. The trailing qualifier
# is a minimum order, not a saving, so only the first number is read. Inferred,
# so flagged as such.
_FLATCODE = re.compile(r"^FLAT(\d{2,4})(?:OFF)?(?:[-_.].*)?$")
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_UPTO = re.compile(r"(?:up\s*to|upto)\s*(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I)
_FLAT = re.compile(r"(?:flat\s*)?(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)\s*off", re.I)
_MINORDER = re.compile(
    r"(?:above|min(?:imum)?(?:\s+order)?(?:\s+value)?(?:\s+of)?)\s*(?:Rs\.?|₹|INR)?\s*(\d+)",
    re.I,
)


# A coupon already on the cart before we touch anything. Probing replaces
# whatever is applied, so this has to be known in order to put it back.
_APPLIED_CODE = (
    # Current food-cart bill shape: ``Coupon (SAVE23): -₹23``.
    re.compile(r"\bcoupon\s*\(\s*([A-Z0-9][A-Z0-9_.+-]{1,49})\s*\)", re.I),
    re.compile(r'\\?"applied_?coupon(?:_?code)?\\?"\s*:\s*\\?"([^"\\]+)', re.I),
    re.compile(r"coupon\s+([A-Z0-9][A-Z0-9_.+-]{1,49})\s+(?:is\s+|successfully\s+)?applied",
               re.I),
    re.compile(r"applied\s+coupon\s*[:\-]?\s*([A-Z0-9][A-Z0-9_.+-]{1,49})", re.I),
)

# How much that coupon is taking off. Without it a swap cannot be costed.
_APPLIED_DISCOUNT = (
    re.compile(
        r'["\']?coupon_?discount["\']?\s*:\s*["\']?'
        r'(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)',
        re.I,
    ),
    re.compile(
        r"\bcoupon\s*\(\s*[A-Z0-9][A-Z0-9_.+-]{1,49}\s*\)\s*:\s*-\s*"
        r"(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)",
        re.I,
    ),
    re.compile(r"coupon\s+discount\D{0,12}(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"(?:you\s+saved|total\s+savings?|savings?)\D{0,12}(?:Rs\.?|₹|INR)\s*"
               r"(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bdiscount\D{0,12}(?:Rs\.?|₹|INR)\s*(\d+(?:\.\d+)?)", re.I),
)


def applied_coupon(text: str) -> str | None:
    """The coupon code already on the cart, if the response names one."""
    for pat in _APPLIED_CODE:
        m = pat.search(text or "")
        if m:
            code = m.group(1).strip().rstrip(_CODE_TRIM)
            if code:
                return code
    return None


def applied_discount(text: str) -> float | None:
    """What the already-applied coupon is taking off, if it is stated."""
    for pat in _APPLIED_DISCOUNT:
        m = pat.search(text or "")
        if m:
            return float(m.group(1))
    return None


def parse_coupons(text: str) -> list[dict]:
    """Extract coupons from a fetch_food_coupons response."""
    if not isinstance(text, str):
        return []

    found: list[dict] = []
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        code = m.group(1).rstrip(_CODE_TRIM)
        flag, rest = m.group(2) or "", m.group(3) or ""
        if not code:
            continue

        # Applicability: explicit flag wins; absent flag is treated as applicable.
        if _NOT_APPLICABLE.search(flag) or _NOT_APPLICABLE.search(rest):
            applicable = False
        elif _APPLICABLE.search(flag):
            applicable = True
        else:
            applicable = True

        shortfall_m = _SHORTFALL.search(rest)
        shortfall = float(shortfall_m.group(1)) if shortfall_m else None

        # Remove the shortfall clause so its rupee figure is never mistaken
        # for a discount.
        cleaned = _SHORTFALL.sub("", rest)

        pct = _PCT.search(cleaned)
        upto = _UPTO.search(cleaned)
        flat = _FLAT.search(cleaned)
        minorder = _MINORDER.search(cleaned)
        uuid = _UUID.search(rest)

        fc = _FLATCODE.match(code)
        found.append({
            "code": code,
            "coupon_id": uuid.group(1) if uuid else None,
            "inferred_flat": float(fc.group(1)) if fc else None,
            "applicable": applicable,
            "shortfall": shortfall,
            "percent": float(pct.group(1)) if pct else None,
            "cap": float(upto.group(1)) if upto else None,
            "flat": float(flat.group(1)) if flat else None,
            "min_order": float(minorder.group(1)) if minorder else None,
            "text": raw.strip(),
        })

    # De-duplicate by code, keeping the most descriptive line.
    best: dict[str, dict] = {}
    for c in found:
        prev = best.get(c["code"])
        if prev is None or len(c["text"]) > len(prev["text"]):
            best[c["code"]] = c
    return list(best.values())


def estimate_discount(coupon: dict, cart_total: float) -> float | None:
    """Estimated rupee saving on this cart, or None when it cannot be known.

    Returns 0.0 for coupons that cannot currently be used.
    """
    if not coupon.get("applicable"):
        return 0.0
    if coupon.get("shortfall"):
        return 0.0
    if coupon.get("min_order") and cart_total < coupon["min_order"]:
        return 0.0

    if coupon.get("percent") is not None:
        value = cart_total * coupon["percent"] / 100.0
        if coupon.get("cap") is not None:
            value = min(value, coupon["cap"])
        return round(value, 2)
    if coupon.get("flat") is not None:
        return round(float(coupon["flat"]), 2)
    return None


def rank(coupons: list[dict], cart_total: float) -> list[dict]:
    """Sort by estimated saving, best first. Unknowable values sink to the end."""
    scored = [
        {**c, "estimated_discount": estimate_discount(c, cart_total)}
        for c in coupons
    ]
    scored.sort(key=lambda c: (c["estimated_discount"] is None, -(c["estimated_discount"] or 0)))
    return scored


def near_misses(coupons: list[dict], applied_code: str | None = None) -> list[dict]:
    """Coupons the user could unlock by spending a bit more.

    Worth surfacing aloud: "spend ₹149 more and you save ₹80".
    """
    out = []
    excluded = (applied_code or "").casefold()
    for c in coupons:
        if excluded and str(c.get("code", "")).casefold() == excluded:
            continue
        if c.get("shortfall"):
            stated = c.get("cap") or c.get("flat")
            out.append({
                "code": c["code"],
                "spend_more": c["shortfall"],
                "would_save": stated or c.get("inferred_flat"),
                # True when the saving came from the code name, not from Swiggy.
                "saving_inferred": stated is None and c.get("inferred_flat") is not None,
                "text": c["text"],
            })
    return sorted(out, key=lambda c: c["spend_more"])


# Bank / card-linked offers. These cannot be redeemed through this CLI at all,
# because the payment surface here is UPI + Cash only - but the user may well
# prefer to pay by card in the Swiggy app and keep the extra discount.
_CARD_HINTS = re.compile(
    r"\b(credit\s*card|debit\s*card|bank\s*offer|cards?\b|"
    r"hdfc|icici|axis|sbi|kotak|amex|american\s*express|citi|citibank|"
    r"yes\s*bank|indusind|rbl|au\s*bank|federal|idfc|standard\s*chartered|"
    r"bob|bank\s*of\s*baroda|onecard|slice|uni\b|paytm\s*postpaid)\b",
    re.I,
)


def card_offers(coupons: list[dict]) -> list[dict]:
    """Coupons that need a specific card or bank to redeem.

    Returned so the caller can WARN the user before paying by UPI: the saving is
    real, but only reachable by paying with that card in the Swiggy app.
    """
    found = []
    for c in coupons:
        blob = f"{c.get('code','')} {c.get('text','')}"
        m = _CARD_HINTS.search(blob)
        if not m:
            continue
        found.append({
            "code": c["code"],
            "matched": m.group(0),
            "potential_saving": c.get("cap") or c.get("flat") or c.get("inferred_flat"),
            "percent": c.get("percent"),
            "applicable": c.get("applicable"),
            "text": c.get("text"),
        })
    return found


def probe_order(ranked: list[dict], extra: int = 4) -> list[dict]:
    """Which coupons to actually attempt, best-first.

    Swiggy's listing cannot be trusted: coupons do not stack on discounted
    items, yet the listing reports a plain value shortfall. So we try every
    coupon the text calls usable, then the most promising `extra` it does not -
    ordered by smallest shortfall, since those are likeliest to succeed.
    """
    usable = [c for c in ranked if c.get("estimated_discount")]
    rest = [c for c in ranked if not c.get("estimated_discount")]
    rest.sort(key=lambda c: (
        c.get("shortfall") if c.get("shortfall") is not None else float("inf"),
        -(c.get("inferred_flat") or 0),
    ))
    return usable + rest[:max(0, extra)]


def pick_best(coupons: list[dict], cart_total: float) -> tuple[dict | None, list[dict]]:
    """Return (best usable coupon or None, ranked list).

    None means nothing is worth applying right now - the caller should say so
    rather than applying a coupon that saves nothing.
    """
    ranked = rank(coupons, cart_total)
    for c in ranked:
        if c["estimated_discount"]:
            return c, ranked
    return None, ranked
