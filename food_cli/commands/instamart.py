"""Instamart groceries."""

from __future__ import annotations

import math
import re

import typer

from ..core import qr as qrmod
from .checkout import (
    _log_order,
    _payment_block,
    _remember_pending_payment,
    _wait_after_order,
    choose_upi_route,
    generic_upi_qr,
    intent_app_choices,
    payment_artifact_guard,
    payment_link_in,
    saved_intent_app,
)
from .common import (
    PAYMENT_REQUIRED_HINT,
    call,
    err,
    extract_payable,
    fee_analysis,
    out,
    response_blob,
    resolve_address,
    _no_open_default,
    _rupees,
)

im_app = typer.Typer(no_args_is_help=True, help="Instamart groceries.")


@im_app.command("search")
def im_search(
    query: str = typer.Argument(...),
    address: str = typer.Option(None, "--address"),
    offset: int = typer.Option(None, "--offset"),
):
    """Search Instamart products."""
    args = {"addressId": resolve_address(address), "query": query}
    if offset is not None:
        args["offset"] = offset
    out(call("instamart", "search_products", args))


@im_app.command("usual")
def im_usual(address: str = typer.Option(None, "--address")):
    """The user's frequently reordered Instamart items."""
    out(call("instamart", "your_go_to_items", {"addressId": resolve_address(address)}))


def _response_dicts(res):
    """Machine-readable MCP payloads, in preferred structured-first order."""
    for channel in ("structuredContent", "content"):
        value = res.get(channel)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if isinstance(candidate, dict):
                yield candidate


def _instamart_data(cart_res) -> dict:
    """The cart's data object regardless of which MCP result channel carried it."""
    for candidate in _response_dicts(cart_res):
        data = candidate.get("data", candidate)
        if isinstance(data, dict) and any(
            key in data for key in ("items", "billBreakdown", "cartTotalAmount", "cartAbsent")
        ):
            return data
    return {}


def _money(value) -> float | None:
    if isinstance(value, dict):
        for key in ("value", "amount", "displayValue", "finalValue", "price"):
            if key in value:
                found = _money(value[key])
                if found is not None:
                    return found
        return None
    return _rupees(value)


def _line_value(line: dict) -> float | None:
    text = " ".join(str(v) for v in line.values())
    if "free" in text.casefold():
        return 0.0
    for key in ("value", "amount", "displayValue", "finalValue", "price"):
        if key in line:
            amount = _money(line[key])
            if amount is not None:
                return amount
    return None


def _instamart_bill_breakdown(cart_res) -> dict:
    """Normalise Instamart's authoritative pre-checkout bill."""
    data = _instamart_data(cart_res)
    items = data.get("items") or []
    if not items and isinstance(data.get("stores"), list):
        items = [
            item
            for store_data in data["stores"] if isinstance(store_data, dict)
            for item in (store_data.get("items") or []) if isinstance(item, dict)
        ]
    subtotal = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        price = _money(item.get("discountedFinalPrice"))
        if price is None:
            price = _money(item.get("mrp")) or 0.0
        quantity = _money(item.get("quantity")) or 1.0
        subtotal += price * quantity
    bill = data.get("billBreakdown") or {}
    raw_lines = bill.get("lineItems") if isinstance(bill, dict) else None
    lines = []
    if isinstance(raw_lines, list):
        for line in raw_lines:
            if not isinstance(line, dict):
                continue
            label = str(line.get("label") or line.get("name") or line.get("title") or "")
            lines.append({"label": label, "value": _line_value(line), "raw": line})

    total = None
    if isinstance(bill, dict):
        total = _money(bill.get("toPay"))
    if total is None:
        total = _money(data.get("cartTotalAmount"))

    delivery = next(
        (line["value"] for line in lines if "delivery" in line["label"].casefold()),
        None,
    )
    cart_absent = bool(data.get("cartAbsent")) or not items
    return {
        "source_tool": "get_cart",
        "complete": (
            not cart_absent
            and bool(items)
            and total is not None
            and isinstance(raw_lines, list)
            and bool(raw_lines)
        ),
        "cart_absent": cart_absent,
        "item_count": len(items),
        "item_subtotal": round(subtotal, 2) if subtotal else None,
        "line_items": lines,
        "delivery_fee": delivery,
        "delivery_is_free": delivery == 0 if delivery is not None else None,
        "payable_total": total,
        "fees_total": (
            round(total - subtotal, 2) if total is not None and subtotal else None
        ),
        "note": "Authoritative pre-order bill from Instamart get_cart.",
    }


def _instamart_fee_analysis(cart_res) -> dict:
    """Derive the fee ratio from Instamart's structured bill."""
    bill = _instamart_bill_breakdown(cart_res)
    return fee_analysis(bill.get("item_subtotal"), bill.get("payable_total"))


def _instamart_placed_total(res) -> float | None:
    """Provider-returned total, including multi-store checkout responses."""
    total_keys = ("cartTotalAmount", "cartTotal", "totalAmount", "payableAmount", "toPay")
    for candidate in _response_dicts(res):
        data = candidate.get("data", candidate)
        if not isinstance(data, dict):
            continue
        for key in total_keys:
            if key in data:
                total = _money(data[key])
                if total is not None:
                    return total
        orders = data.get("orders") or data.get("results")
        if isinstance(orders, list) and orders:
            totals = []
            for order in orders:
                if not isinstance(order, dict):
                    totals = []
                    break
                amount = None
                for key in total_keys:
                    if key in order:
                        amount = _money(order[key])
                        if amount is not None:
                            break
                if amount is None:
                    totals = []
                    break
                totals.append(amount)
            if totals:
                return round(sum(totals), 2)
    return extract_payable(response_blob(res))


@im_app.command("cart")
def im_cart():
    """Show the Instamart cart, with a fee breakdown."""
    res = call("instamart", "get_cart", {})
    bill = _instamart_bill_breakdown(res)
    out({
        **res,
        "checkout_preview": {
            **bill,
            "approval_note": (
                "Approve only when complete is true, after hearing the items, "
                "delivery fee, address, and exact payable_total."
            ),
        },
        "fee_analysis": _instamart_fee_analysis(res),
    })


@im_app.command("payment-options")
def im_payment_options(address: str = typer.Option(None, "--address")):
    """Payment methods available for the current Instamart cart.

    The list is eligibility-filtered per account and per cart, so a wallet
    (SwiggyPay / Swiggy Money) only appears when it is actually usable. Always
    read it live rather than assuming.
    """
    # Instamart resolves cart context server-side; the MCP contract has no
    # input parameters for this read.
    res = call("instamart", "get_payment_options", {})
    blob = response_blob(res)
    groups = sorted(set(re.findall(r'"groupName":\s*"([^"]+)"', blob)))
    out({
        **res,
        "groups": groups,
        "generic_upi_qr": generic_upi_qr(res),
        "upi_apps": intent_app_choices(res),
        "preferred_upi_app": saved_intent_app(),
        "wallet_available": any(g.lower() in ("swiggypay", "wallet", "swiggymoney") for g in groups),
        "cod_available": any(g.upper() == "COD" for g in groups),
        "hands_free_possible": any(g.upper() == "COD" for g in groups),
    })


@im_app.command("fees")
def im_fees():
    """Explain the current fee position and what would remove it."""
    out(_instamart_fee_analysis(call("instamart", "get_cart", {})))


@im_app.command("add")
def im_add(
    item: list[str] = typer.Option(..., "--item", help="spinId:quantity (repeatable)."),
    address: str = typer.Option(None, "--address"),
):
    """Add items to the Instamart cart. --item SPIN123:2"""
    items = []
    for spec in item:
        spin, _, qty = spec.partition(":")
        items.append({"spinId": spin, "quantity": int(qty or 1)})
    out(call("instamart", "update_cart", {
        "selectedAddressId": resolve_address(address),
        "items": items,
    }))


@im_app.command("clear")
def im_clear():
    """Empty the Instamart cart."""
    out(call("instamart", "clear_cart", {}))


@im_app.command("checkout")
def im_checkout(
    address: str = typer.Option(None, "--address"),
    payment: str = typer.Option(
        None, "--payment",
        help='Required. "UPI" or "Cash"/"COD". Ask the user - never assume, and '
             'never default to Cash on delivery.',
    ),
    intent_app: str = typer.Option(
        None, "--intent-app",
        help="The UPI app the user chose, by provider id or exact display name. "
             "A valid explicit choice is saved for later orders.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Required. Confirms you intend to spend money."),
    max_total: float = typer.Option(
        None, "--max-total",
        help="Required. Most rupees approved after reviewing the final Instamart cart.",
    ),
    ignore_fees: bool = typer.Option(
        False, "--ignore-fees", help="Proceed even when fees are disproportionate."
    ),
    no_open: bool = typer.Option(False, "--no-open", help="Do not auto-open the UPI QR image."),
    wait: bool = typer.Option(
        False, "--wait",
        help="After checkout, watch the payment through to confirmation.",
    ),
    wait_timeout: int = typer.Option(300, "--wait-timeout"),
):
    """Check out Instamart with explicit payment and an approved price ceiling."""
    if not payment:
        err(PAYMENT_REQUIRED_HINT.format(group="im"))
        raise typer.Exit(2)
    if not yes:
        err("Refusing to check out without --yes/-y. Confirm the cart and total with the user first.")
        raise typer.Exit(2)
    if max_total is None or not math.isfinite(max_total) or max_total <= 0:
        err(
            "Refusing to check out without a positive --max-total. Show the final "
            "Instamart cart, get approval, and pass its exact payable_total."
        )
        raise typer.Exit(2)

    # Bind -y to the exact bill the user heard. get_cart is the only
    # authoritative pre-order source for Instamart fees and payable total.
    cart = call("instamart", "get_cart", {})
    bill = _instamart_bill_breakdown(cart)
    live_total = bill.get("payable_total")
    if bill.get("cart_absent"):
        out({"status": "blocked_empty_cart", "checkout_preview": bill})
        raise typer.Exit(5)
    if not bill.get("complete") or live_total is None:
        err("Refusing checkout: Instamart did not return a complete bill breakdown.")
        out({
            "status": "blocked_incomplete_bill_breakdown",
            "approved_max_total": max_total,
            "checkout_preview": bill,
            "stage": "preflight",
        })
        raise typer.Exit(5)
    if live_total > max_total + 0.009:
        err(
            f"Refusing checkout: live payable is ₹{live_total:.2f}, above the "
            f"approved ₹{max_total:.2f}. Show the changed cart and ask again."
        )
        out({
            "status": "blocked_total_changed",
            "approved_max_total": max_total,
            "live_total": live_total,
            "checkout_preview": bill,
            "increase": round(live_total - max_total, 2),
            "stage": "preflight",
        })
        raise typer.Exit(5)
    if live_total >= 1000:
        err("Instamart MCP checkout only supports carts below ₹1000; use the app for this cart.")
        out({
            "status": "blocked_provider_cart_limit",
            "live_total": live_total,
            "limit": 1000,
            "checkout_preview": bill,
        })
        raise typer.Exit(5)

    # Always flag bad fee ratios before spending money.
    fees = _instamart_fee_analysis(cart)
    if fees.get("known") and (fees.get("high_fees") or fees.get("spend_more_to_save")):
        err(f"\n⚠️  {fees['advice']}\n")
        if not ignore_fees:
            err("Not checking out. Add more items, or re-run with --ignore-fees to accept the fees.")
            out({"status": "blocked_high_fees", "fee_analysis": fees})
            raise typer.Exit(3)

    args: dict = {"addressId": resolve_address(address)}
    intent_choice: dict | None = None
    if payment:
        args["paymentMethod"] = payment
    if payment.strip().upper() == "UPI":
        route, why = choose_upi_route("instamart", args["addressId"], intent_app)
        chosen = (route or {}).get("intentApp")
        intent_choice = {"requested": intent_app, "used": chosen, **why}
        if not route:
            err(
                f"\nRefusing to check out: {why['reason']}.\n"
                "Show the available UPI apps to the user, ask which one they "
                "prefer, then pass it with --intent-app.\n"
                "    food im payment-options\n"
            )
            status = (
                "blocked_upi_app_choice" if why.get("requires_choice")
                else "blocked_no_payable_upi"
            )
            out({"status": status, "intent_app_choice": why})
            raise typer.Exit(3)
        args.update(route)
        if why.get("mode") == "generic_qr":
            err("Using provider-supported generic UPI QR; no app choice is required.")
        else:
            err(f"Using {why['selected']['name']} for UPI - {why['reason']}.")
    elif intent_app:
        err("--intent-app is only valid together with --payment UPI.")
        raise typer.Exit(2)

    res = call("instamart", "checkout", args)
    placed_total = _instamart_placed_total(res)
    if placed_total is None or placed_total > max_total + 0.009:
        status = (
            "blocked_unverified_placed_total"
            if placed_total is None else "blocked_total_changed"
        )
        err(
            "Instamart checkout did not return the approved total unchanged. "
            "No payment artifact will be exposed; do not pay this attempt."
        )
        out({
            "status": status,
            "approved_max_total": max_total,
            "preflight_total": live_total,
            "placed_total": placed_total,
            "preflight_bill_breakdown": bill,
            "stage": "post_placement",
            "payment_suppressed": True,
            "provider_response_suppressed": True,
            "action": "Do not pay; let a pending attempt expire and review a fresh cart.",
        })
        raise typer.Exit(5)

    link = payment_link_in(res)
    found = qrmod.find_qr(res)
    payment_guard = payment_artifact_guard(found, placed_total) if found else None
    if payment_guard and not payment_guard["safe_to_present"]:
        status = (
            "blocked_payment_amount_mismatch"
            if payment_guard.get("amount") is not None
            and payment_guard.get("expected_total") is not None
            else "blocked_unverified_payment_amount"
        )
        err(
            f"{payment_guard['reason']}. No QR or payment link will be exposed; "
            "do not pay this attempt."
        )
        out({
            "status": status,
            "approved_max_total": max_total,
            "preflight_total": live_total,
            "placed_total": placed_total,
            "payment_artifact": payment_guard,
            "stage": "post_placement",
            "payment_suppressed": True,
            "provider_response_suppressed": True,
            "action": "Do not pay; let a pending attempt expire and review a fresh cart.",
        })
        raise typer.Exit(5)

    _log_order("instamart", res, args.get("addressId"))
    payload = {**res}
    payload["price_guard"] = {
        "approved_max_total": max_total,
        "preflight_total": live_total,
        "placed_total": placed_total,
        "preflight_bill_breakdown": bill,
        "payment_artifact": payment_guard,
        "verified": True,
    }
    if intent_choice:
        payload["intent_app_choice"] = intent_choice
    if link:
        payload["payment_link"] = link
    oid = qrmod.extract_order_id(res) or "instamart-order"
    if found:
        payload["qr"] = qrmod.present(found, order_ref=oid,
                                      open_browser=not _no_open_default(no_open))
        _remember_pending_payment(
            "instamart", oid, found,
            {
                **payload["qr"],
                "payment_link": link,
                "expected_amount": placed_total,
                "amount_verified": True,
            },
        )
    payload["payment"] = _payment_block(res, payload.get("qr"), link, oid)
    if not found:
        err("\n>>> Payment must be completed by the USER in their UPI app.\n")
    if wait:
        _wait_after_order("instamart", res, payload, args["addressId"], wait_timeout)
    out(payload)
