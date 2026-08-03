"""Shared helpers every command group needs.

`call` is the single choke point through which every MCP request passes, which
is also where the test suite substitutes its fake transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import typer

from ..core import store
from ..mcp import client


def out(data) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Some servers report failure in prose while still setting isError=false, so a
# naive caller reads "Error: ... Too Many Requests" as a successful result.
# These are matched against the start of the text only, so a product genuinely
# named "Error" in a search result cannot trip them.
_UPSTREAM_ERROR = re.compile(r"^\s*(?:error|failed)\b[:\-]?\s*(?P<detail>.+)", re.I)
_RATE_LIMITED = re.compile(r"too many requests|rate.?limit", re.I)


def upstream_error(res) -> str | None:
    """The error a tool reported in prose, if it did.

    Returns None for a healthy response.
    """
    if res.get("isError"):
        return text_of(res)[:300]
    body = res.get("content")
    if not isinstance(body, str):
        return None
    m = _UPSTREAM_ERROR.match(body)
    return m.group("detail")[:300] if m else None


def call(server: str, tool: str, args: dict):
    """Every MCP request goes through here - including the test suite's fake.

    A prose-reported failure is annotated rather than raised: the raw content
    still reaches the caller, but nothing downstream can mistake it for data.
    """
    res = asyncio.run(client.call_tool(server, tool, args))
    detail = upstream_error(res)
    if detail:
        res = {**res, "upstream_error": detail,
               "rate_limited": bool(_RATE_LIMITED.search(detail))}
    return res


def resolve_address(explicit: str | None) -> str:
    """addressId is required by nearly every tool. Never guess it."""
    if explicit:
        return explicit
    default = store.get_pref("default_address_id")
    if default:
        return default
    err(
        "No delivery address set. Run:\n"
        "    food address list\n"
        "    food address set-default <addressId>\n"
    )
    raise typer.Exit(2)


def text_of(res) -> str:
    c = res.get("content")
    return c if isinstance(c, str) else json.dumps(c, default=str)


def response_blob(res) -> str:
    """All model-visible provider data as text, including structured JSON.

    MCP Apps keep their useful machine-readable result in ``structuredContent``.
    The CLI does not render the associated widget, so payment/status extractors
    must inspect this channel as well as the conversational fallback text.
    Private result metadata is intentionally not included by the transport.
    """
    return json.dumps({
        "content": res.get("content"),
        "structuredContent": res.get("structuredContent"),
    }, ensure_ascii=False, default=str)


# Measured empirically on Instamart: a small basket pays a large flat fee,
# and crossing roughly Rs 199 of goods collapses it to a token amount.
# This varies by city, account and membership plan, so it is only a default -
# override per user with `food config free_delivery_threshold <n>`.
DEFAULT_FREE_DELIVERY_THRESHOLD = 199.0
# Flag when fees are this fraction of the goods - a bad-value order.
FEE_RATIO_WARN = 0.25
# Only halt for a coupon top-up if the extra spend is at most this multiple of
# the saving. Beyond that the "offer" costs more than it returns.
NEAR_MISS_MAX_RATIO = 2.5

# Swiggy's own instruction: "Never assume or default a payment method" and
# "Never place the order without the user confirming the method." Omitting it
# lets the server pick one silently - including Cash on delivery, which commits
# the user to paying a courier they never agreed to. So it must be explicit.
PAYMENT_REQUIRED_HINT = (
    "Refusing to order without an explicit --payment.\n"
    "The provider will silently pick one otherwise, which may not be what the "
    "user wants.\n"
    "  1. food {group} payment-options      # what this account can actually use\n"
    "  2. ask the user which one\n"
    "  3. re-run with --payment UPI  (or Cash, if they chose cash on delivery)"
)
# Providers tell agents not to poll because their own payment widget does it for
# them. A CLI has no widget, so if we do not poll, nothing does and the order
# sits pending forever. We take that job on - at the cadence the response asks
# for, with this as the floor so we are never the tight loop they warn about.
MIN_POLL_INTERVAL = 15.0
def fee_analysis(subtotal: float | None, total: float | None, kind: str = "instamart") -> dict:
    """Work out whether the user is about to overpay in fees."""
    if subtotal is None or total is None or subtotal <= 0:
        return {"known": False}

    fees = round(total - subtotal, 2)
    ratio = fees / subtotal
    threshold = float(store.get_pref("free_delivery_threshold", DEFAULT_FREE_DELIVERY_THRESHOLD))
    info = {
        "known": True,
        "subtotal": subtotal,
        "total": total,
        "fees": fees,
        "fee_ratio": round(ratio, 3),
        "free_delivery_threshold": threshold,
        "high_fees": ratio >= FEE_RATIO_WARN,
    }
    if subtotal < threshold:
        info["spend_more_to_save"] = round(threshold - subtotal, 2)
        info["advice"] = (
            f"Fees are ₹{fees:.0f} on ₹{subtotal:.0f} of goods. "
            f"Add ₹{threshold - subtotal:.0f} more to cross the ₹{threshold:.0f} "
            f"free-delivery threshold and most of that fee disappears."
        )
    elif info["high_fees"]:
        info["advice"] = f"Fees are ₹{fees:.0f} on ₹{subtotal:.0f} of goods - unusually high."
    return info


_TOTAL = re.compile(
    r"(?:cart\s+total|grand\s+total|total\s+amount|to\s+pay|payable|total)\D{0,15}"
    r"(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)",
    re.I,
)

# The amount actually charged, in priority order. "Item total" is the pre-tax
# subtotal and must never be compared against a post-coupon "New total" - doing
# so mixes two different figures and misreports the saving.
_PAYABLE = (
    re.compile(r"new\s+total\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"to\s*pay\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"grand\s+total\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"payable\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"total\s+paid\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    # Structured provider responses use camelCase or snake_case fields. Keep
    # these explicit: a generic `total` pattern also matches `Item total`,
    # which is only the pre-tax/pre-delivery subtotal.
    re.compile(
        r'["\']?(?:cart_?total(?:_?amount)?|total_?amount|total_?paid|paid_?amount)'
        r'["\']?\s*:\s*'
        r'["\']?(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)',
        re.I,
    ),
    re.compile(r"cart\s+total\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"total\s+amount\D{0,10}(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)", re.I),
)


def extract_total(text: str) -> float | None:
    m = _TOTAL.search(_currency_text(text))
    return float(m.group(1)) if m else None


def extract_payable(text: str) -> float | None:
    """The amount that will actually be charged.

    Coupon responses report a post-tax "New total"; the cart reports both an
    "Item total" and a "TO PAY". Comparing the wrong pair silently misreports
    every saving, so this only ever returns a payable figure.
    """
    searchable = _currency_text(text)
    for pat in _PAYABLE:
        m = pat.search(searchable)
        if m:
            return float(m.group(1))
    return None


_DELIVERY_FEE = re.compile(
    r"\bdelivery(?:\s+(?:fee|charge))?\s*:\s*"
    r"(?:(FREE)|(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?))",
    re.I,
)


def extract_delivery_fee(text: str) -> float | None:
    """Delivery charge from a cart/bill, with ``FREE`` represented as zero."""
    m = _DELIVERY_FEE.search(_currency_text(text))
    if not m:
        return None
    return 0.0 if m.group(1) else float(m.group(2))


_TAXES_AND_CHARGES = (
    re.compile(
        r"\btax(?:es)?\s*(?:&|and)?\s*charges?\s*:\s*"
        r"(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)",
        re.I,
    ),
    re.compile(
        r'["\']?(?:taxes?_?and_?charges?|taxes?_?charges?|tax_?amount)'
        r'["\']?\s*:\s*["\']?(?:Rs\.?|₹|INR)?\s*(\d+(?:\.\d+)?)',
        re.I,
    ),
)


def extract_taxes_and_charges(text: str) -> float | None:
    """The cart's combined tax/charges line, when Swiggy itemises it."""
    searchable = _currency_text(text)
    for pat in _TAXES_AND_CHARGES:
        m = pat.search(searchable)
        if m:
            return float(m.group(1))
    return None


def _currency_text(text: str | None) -> str:
    """Make rupee symbols searchable in both prose and JSON-escaped blobs."""
    return (text or "").replace(r"\u20b9", "₹").replace(r"\u20B9", "₹")


def _rupees(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


def _no_open_default(flag: bool) -> bool:
    """Respect FOOD_CLI_NO_OPEN for headless and agent-driven runs.

    SWIGGY_CLI_NO_OPEN is still honoured so existing setups keep working.
    """
    if flag:
        return True
    for var in ("FOOD_CLI_NO_OPEN", "SWIGGY_CLI_NO_OPEN"):
        if os.environ.get(var, "").lower() in ("1", "true", "yes"):
            return True
    return False
