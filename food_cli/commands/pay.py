"""Payment: QR, status, confirmation."""

from __future__ import annotations

import typer

from ..core import qr as qrmod, store
from .checkout import (
    _log_order,
    _payment_block,
    payment_artifact_guard,
    wait_for_payment,
)
from .common import (
    MIN_POLL_INTERVAL,
    call,
    err,
    out,
    resolve_address,
)

pay_app = typer.Typer(no_args_is_help=True, help="Payment: QR, status, confirmation.")


@pay_app.command("qr")
def pay_qr(
    source: str = typer.Argument(
        None,
        help="upi:// intent, a Swiggy redirect URL, or an order id. "
             "Omit to re-show the QR for the last pending payment.",
    ),
    order_id: str = typer.Option(None, "--order-id", help="Name the file by this order id."),
    kind: str = typer.Option(None, "--kind", help="food | instamart, with no source."),
    open_image: bool = typer.Option(True, "--open/--no-open"),
):
    """Render a UPI QR: prints it, saves PNG+SVG under ~/.food-cli/qr/<orderId>.

    With no arguments this re-shows the QR for the most recent pending payment,
    which is the usual case: an order came back PENDING_PAYMENT and the QR needs
    showing again. It never creates or invents payment data - it only re-renders
    the intent Swiggy already issued.
    """
    stored_link = None
    expected_amount = None
    if source is None:
        key = f"pending_payment_{kind}" if kind else "pending_payment_last"
        rec = store.get_pref(key)
        if not rec:
            err("No pending payment recorded. Pass a upi:// intent, a redirect "
                "URL, or an order id.")
            raise typer.Exit(2)
        if rec.get("amount_verified") is not True or rec.get("expected_amount") is None:
            out({
                "status": "blocked_unverified_payment_amount",
                "order_id": rec.get("order_id"),
                "payment_suppressed": True,
                "action": "Re-read the live order/cart; do not render this stored payment.",
            })
            raise typer.Exit(5)
        expected_amount = float(rec["expected_amount"])
        if rec.get("png") and __import__("pathlib").Path(rec["png"]).exists():
            err(f"\nQR already saved for order {rec.get('order_id')}:\n    {rec['png']}\n")
        stored_link = rec.get("source_url")
        source = rec.get("upi_uri") or rec.get("source_url") or ""
        order_id = order_id or rec.get("order_id")
        if not source:
            out({"status": "no_intent_stored", **rec})
            raise typer.Exit(1)

    if source.startswith("upi://") or source.startswith("http"):
        found = qrmod.find_qr(source)
    else:
        # Treat as an order id and look up the stored payload.
        rows = [o for o in store.list_orders(limit=200) if o["id"] == source]
        if not rows:
            err(f"No stored order {source}.")
            raise typer.Exit(2)
        with store.connect() as c:
            raw = c.execute("SELECT payload FROM orders WHERE id=?", (source,)).fetchone()
        found = qrmod.find_qr(raw["payload"] if raw else "")
        expected_amount = rows[0].get("amount")
        order_id = order_id or source

    if not found:
        err("No UPI QR found in that input.")
        raise typer.Exit(1)
    if expected_amount is not None:
        guard = payment_artifact_guard(found, float(expected_amount))
        if not guard["safe_to_present"]:
            out({
                "status": (
                    "blocked_payment_amount_mismatch"
                    if guard.get("amount") is not None
                    else "blocked_unverified_payment_amount"
                ),
                "order_id": order_id,
                "payment_artifact": guard,
                "payment_suppressed": True,
                "action": "Do not pay or render this stored payment artifact.",
            })
            raise typer.Exit(5)
    presented = qrmod.present(found, order_ref=order_id or "payment",
                              open_browser=open_image)
    # An https link survives chat clients that will not linkify a upi:// scheme,
    # so carry it through here too - it is often the only tappable artefact.
    link = stored_link or found.get("source_url")
    if not link and source.startswith("http"):
        link = source
    out({**presented, "payment": _payment_block({}, presented, link, order_id)})


@pay_app.command("status")
def pay_status(
    paas_id: str = typer.Argument(..., help="paasId from the checkout response."),
    order_id: str = typer.Option(None, "--order-id"),
    kind: str = typer.Option("instamart", "--kind", help="food | instamart"),
    address: str = typer.Option(None, "--address"),
):
    """Check whether a UPI payment has gone through."""
    args: dict = {"paasId": paas_id}
    if order_id:
        args["orderId"] = order_id
    args["addressId"] = resolve_address(address)
    out(call("instamart" if kind == "instamart" else "food", "check_payment_status", args))


@pay_app.command("wait")
def pay_wait(
    paas_id: str = typer.Argument(None, help="paasId; defaults to the last order's."),
    order_id: str = typer.Option(None, "--order-id"),
    kind: str = typer.Option("instamart", "--kind", help="food | instamart"),
    address: str = typer.Option(None, "--address"),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to keep polling."),
    interval: float = typer.Option(
        0, "--interval",
        help="Seconds between checks. 0 follows the interval Swiggy asks for "
             f"(min {MIN_POLL_INTERVAL:g}s).",
    ),
    auto_confirm: bool = typer.Option(
        True, "--auto-confirm/--no-auto-confirm",
        help="Call confirm_order on success if Swiggy has not already.",
    ),
):
    """Watch a pending payment through to confirmation.

    Stands in for Swiggy's payment widget, which is what normally does this in
    a browser. Safe to run while the user pays: it only reads status until the
    payment is terminal.
    """
    paas_id = paas_id or store.get_pref(f"last_paas_id_{kind}")
    order_id = order_id or store.get_pref(f"last_order_id_{kind}")
    if not paas_id:
        err("No paasId given and none stored. Pass it explicitly.")
        raise typer.Exit(2)

    def tick(n, remaining, every):
        err(f"  … still pending (check {n}, {remaining}s left, next in {every:.0f}s)")

    res = wait_for_payment(kind, paas_id, order_id, resolve_address(address),
                           timeout=timeout, interval=interval,
                           auto_confirm=auto_confirm, on_tick=tick)
    if res["status"] == "paid":
        err("\n✅ Payment successful — order confirmed.\n")
        out(res)
        return
    if res["status"] == "failed":
        err("\n❌ Payment failed.\n")
        out(res)
        raise typer.Exit(1)
    err(f"\n⏳ Still pending after {timeout}s — not necessarily failed.\n")
    out(res)
    raise typer.Exit(2)


@pay_app.command("confirm")
def pay_confirm(
    order_id: str = typer.Argument(...),
    paas_id: str = typer.Option(None, "--paas-id"),
    kind: str = typer.Option("instamart", "--kind"),
    address: str = typer.Option(None, "--address"),
):
    """Finalise an order after payment succeeded."""
    args: dict = {"orderId": order_id}
    if paas_id:
        args["paasId"] = paas_id
    args["addressId"] = resolve_address(address)
    res = call("instamart" if kind == "instamart" else "food", "confirm_order", args)
    _log_order(kind, res, args.get("addressId"))
    out(res)
