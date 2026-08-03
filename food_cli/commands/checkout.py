"""Order and payment plumbing shared by food, Instamart and `pay`.

None of this is a CLI command. It is the machinery that replaces the browser
payment widget: identifying an order, remembering what is pending, polling the
payment through to a terminal state, and logging the result.
"""

from __future__ import annotations

import re
import time

from ..core import profile, store
from ..core import qr as qrmod
from ..providers import SERVERS
from .common import MIN_POLL_INTERVAL, call, err, extract_payable, response_blob, text_of

__all__ = [
    "wait_for_payment", "order_id_in", "_payment_block", "_log_order",
    "_remember_pending_payment", "_wait_after_order", "_mark_order_status",
    "payment_artifact_guard", "payment_option_amount",
]


_ORDER_ID_PATTERNS = (
    # JSON field, e.g. "orderId": "1234567890"
    re.compile(r'\\?"?order_?id\\?"?\s*:\s*\\?"?([A-Za-z0-9\-]{6,})', re.I),
    # Prose, e.g. "Order 1234567890 - Sample Diner" or "order id: ABC123"
    re.compile(r"\border\s*(?:id|#)?\s*[:\-]?\s*([0-9]{8,})\b", re.I),
)
# May be plain, quoted, or JSON-escaped depending on where it appears.
_PAAS_ID = re.compile(r'\\?"?paas_?id\\?"?\s*:\s*\\?"?([A-Za-z0-9\-]{6,})', re.I)
_CART_TOTAL = re.compile(r'"cart_?total"\s*:\s*"?₹?\s*(\d+(?:\.\d+)?)', re.I)
_STATUS = re.compile(r'"status"\s*:\s*"([A-Z_]+)"')


def order_id_in(text: str) -> str | None:
    """First thing in the text that looks like an order id."""
    for pat in _ORDER_ID_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1)
    return None


def _server_for(kind: str) -> str:
    """The MCP server that owns a given order kind.

    `kind` is how orders are labelled locally ("food", "instamart", "zepto");
    for every provider so far that is also the server key, but fall back to food
    rather than raising on an unknown label.
    """
    return kind if kind in SERVERS else "food"


# A UPI app id is a custom scheme, e.g. "gpay://upi/" or "phonepe://".
_APP_SCHEME = re.compile(r"^[a-z][a-z0-9.+-]*://")


# The https link Swiggy issues for a pending payment. It appears under several
# names depending on the endpoint, so all of them are tried - this link is often
# the only artefact a chat client can actually deliver.
_PAYMENT_LINK_PATTERNS = (
    re.compile(r"Payment link:\s*(https://\S+)", re.I),
    re.compile(r'\\?"(?:bridgeUrl|paymentLink|payment_?url|deeplink)\\?"\s*:\s*'
               r'\\?"(https://[^"\\]+)', re.I),
    re.compile(r"(https://mcp\.swiggy\.com/\S+)", re.I),
)


_PAYMENT_LINK_FIELDS = {
    "bridgeurl", "paymentlink", "payment_link", "paymenturl", "payment_url",
    "deeplink", "sourceurl", "source_url",
}


def _named_values(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).casefold(), nested
            yield from _named_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _named_values(nested)


def payment_link_in(payload) -> str | None:
    """The HTTPS payment page for a pending order, including structured JSON."""
    if not isinstance(payload, str):
        for key, value in _named_values(payload):
            if key in _PAYMENT_LINK_FIELDS and isinstance(value, str):
                if value.startswith("https://"):
                    return value.rstrip('",\\')
        text = response_blob(payload)
    else:
        text = payload
    for pat in _PAYMENT_LINK_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1).rstrip('",\\')
    return None


def payment_artifact_guard(found: dict | None, expected_total: float | None) -> dict:
    """Verify a decoded UPI intent charges exactly the provider-approved total.

    Swiggy can return a current ``totalAmount`` alongside a stale UPI intent
    from another cart. The intent amount is what the user's bank will actually
    authorise, so it must be checked before a QR or bridge link is exposed.
    """
    kind = (found or {}).get("kind")
    amount = (
        qrmod._amount_of(found.get("value", ""))  # noqa: SLF001 - shared parser
        if kind == "upi_uri" else None
    )
    verified = (
        expected_total is not None
        and amount is not None
        and round(amount, 2) == round(expected_total, 2)
    )
    if not found:
        reason = "provider returned no payment artifact"
    elif amount is None:
        reason = "payment artifact amount could not be verified"
    elif expected_total is None:
        reason = "provider order total could not be verified"
    elif not verified:
        reason = (
            f"payment artifact requests ₹{amount:.2f}, but the verified order "
            f"total is ₹{expected_total:.2f}"
        )
    else:
        reason = None
    return {
        "kind": kind,
        "amount": amount,
        "expected_total": expected_total,
        "verified": verified,
        "safe_to_present": verified,
        "reason": reason,
    }


def _result_dicts(res):
    for channel in ("structuredContent", "content"):
        content = res.get(channel)
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, dict):
                yield block
                data = block.get("data")
                if isinstance(data, dict):
                    yield data


def payment_methods(res) -> list[dict]:
    """The `allMethods` list out of a get_payment_options response."""
    for block in _result_dicts(res):
        if isinstance(block, dict) and isinstance(block.get("allMethods"), list):
            return block["allMethods"]
    return []


def payment_option_amount(res) -> float | None:
    """Return the provider's payment-picker quote when it publishes one.

    Food documents ``paymentAmount`` as the current cart total.  It is an
    independent read from ``get_food_cart`` and must agree before a UPI order
    can be created.  Instamart does not promise this field, so callers treat a
    missing value there as unavailable rather than inventing one.
    """
    for block in _result_dicts(res):
        if not isinstance(block, dict):
            continue
        for key in ("paymentAmount", "payment_amount"):
            value = block.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


UPI_APP_PREF_KEY = "preferred_upi_app"


def intent_app_choices(res) -> list[dict]:
    """Enabled named UPI apps suitable for an ``intentApp`` argument."""
    return [
        {
            "id": str(m.get("id")),
            "name": str(m.get("displayName") or m.get("name") or m.get("id")),
        }
        for m in payment_methods(res)
        if m.get("enabled") and _APP_SCHEME.match(str(m.get("id", "")))
    ]


def saved_intent_app() -> dict | None:
    """The UPI app the user explicitly chose previously, if any."""
    saved = store.get_pref(UPI_APP_PREF_KEY)
    if isinstance(saved, str):
        return {"id": saved, "name": saved}
    return saved if isinstance(saved, dict) and saved.get("id") else None


def generic_upi_qr(res) -> dict | None:
    """The provider-advertised desktop ``PayWithQR`` capability, if enabled."""
    for block in _result_dicts(res):
        platforms = block.get("platforms") or {}
        desktop = platforms.get("desktop") or {}
        for method in desktop.get("methods") or []:
            if not isinstance(method, dict) or method.get("enabled") is False:
                continue
            if str(method.get("id", "")).casefold() == "paywithqr":
                return {
                    "id": str(method["id"]),
                    "name": str(method.get("displayName") or method.get("name")
                                or "Scan QR with any UPI app"),
                }
    return None


def _payment_options_with_retry(server: str, addr: str) -> tuple[dict | None, dict | None]:
    """Read live payment capabilities, retrying transient MCP failures."""
    res = None
    last_error = "unknown error"
    attempts = 3
    for attempt in range(attempts):
        try:
            # Food requires/echoes an address. Instamart resolves its cart
            # server-side and its public contract takes no arguments.
            arguments = {"addressId": addr} if server == "food" else {}
            candidate = call(server, "get_payment_options", arguments)
            detail = candidate.get("upstream_error")
            if not detail and not candidate.get("isError"):
                res = candidate
                break
            last_error = str(detail or text_of(candidate))[:120]
        except Exception as e:  # noqa: BLE001 - MCP transport failures are transient
            last_error = str(e)[:120]
        if attempt < attempts - 1:
            time.sleep(0.1 * (attempt + 1))

    if res is None:
        return None, {
            "reason": (
                f"could not read payment options after {attempts} attempts: "
                f"{last_error}"
            ),
            "available": [],
            "requires_choice": False,
            "attempts": attempts,
        }
    return res, None


def _intent_app_from_options(res, requested: str | None = None) -> tuple[str | None, dict]:
    """Select an explicit or saved app from one live options response."""

    choices = intent_app_choices(res)
    if not choices:
        return None, {
            "reason": "no UPI app intent is enabled for this cart",
            "available": [],
            "requires_choice": False,
        }

    saved = saved_intent_app()
    candidate = requested or (saved or {}).get("id")
    source = "requested" if requested else "saved"
    if not candidate:
        return None, {
            "reason": "choose a UPI app before placing the order",
            "available": choices,
            "requires_choice": True,
        }

    # The CLI normally passes the provider id, but accepting an exact display
    # name lets an interactive caller pass "Paytm" instead of a custom scheme.
    match = next((c for c in choices if c["id"] == candidate), None)
    if match is None and requested:
        matches = [c for c in choices if c["name"].casefold() == requested.casefold()]
        match = matches[0] if len(matches) == 1 else None
    if match is None:
        return None, {
            "reason": f"the {source} UPI app is not enabled for this cart",
            "requested": candidate,
            "available": choices,
            "requires_choice": True,
        }

    if requested:
        store.set_pref(UPI_APP_PREF_KEY, match)
    return match["id"], {
        "reason": f"using the user's {source} UPI app",
        "selected": match,
        "available": choices,
        "requires_choice": False,
        "saved": bool(requested),
    }


def choose_intent_app(server: str, addr: str,
                      requested: str | None = None) -> tuple[str | None, dict]:
    """Use an explicit or saved UPI app, never an assumed app."""
    res, failure = _payment_options_with_retry(server, addr)
    if res is None:
        return None, failure
    return _intent_app_from_options(res, requested)


def choose_upi_route(server: str, addr: str,
                     requested: str | None = None) -> tuple[dict | None, dict]:
    """Choose a live UPI route without inventing provider capability.

    An explicit app wins. Otherwise use provider-advertised desktop PayWithQR,
    which is the only generic UPI contract. If unavailable, fall back to the
    user's saved app or require a fresh app choice.
    """
    res, failure = _payment_options_with_retry(server, addr)
    if res is None:
        return None, failure

    quote = payment_option_amount(res)
    if requested:
        app, detail = _intent_app_from_options(res, requested)
        return ({"intentApp": app} if app else None), {
            **detail,
            "mode": "app_intent" if app else None,
            "payment_amount": quote,
        }

    generic = generic_upi_qr(res)
    if generic:
        return {"generateUPIQR": True}, {
            "reason": "provider advertised generic desktop UPI QR",
            "mode": "generic_qr",
            "selected": generic,
            "available": intent_app_choices(res),
            "requires_choice": False,
            "saved": False,
            "payment_amount": quote,
        }

    app, detail = _intent_app_from_options(res, None)
    return ({"intentApp": app} if app else None), {
        **detail,
        "mode": "app_intent" if app else None,
        "payment_amount": quote,
    }


def choose_food_upi_route(addr: str,
                          requested: str | None = None) -> tuple[dict | None, dict]:
    """Backward-compatible Food wrapper around the shared UPI router."""
    return choose_upi_route("food", addr, requested)


def _payment_block(res, presented: dict | None, payment_link: str | None,
                   order_id: str | None) -> dict:
    """One flat place for everything needed to pay.

    A caller should not have to dig through nested tool output to find the
    intent or the image - especially a voice agent, which needs the UPI id as
    text and the PNG as an attachment.
    """
    presented = presented or {}
    return {
        "order_id": order_id,
        "app_intent": presented.get("upi_uri"),
        "upi_id": presented.get("payee"),
        "amount": presented.get("amount"),
        "qr_png": presented.get("png"),
        "qr_svg": presented.get("svg"),
        "payment_link": payment_link,
        "status": "PENDING_PAYMENT" if "PENDING_PAYMENT" in response_blob(res) else None,
        "note": (
            "Attach or render qr_png; read upi_id and amount aloud. Never read "
            "a file path to the user. Payment is completed by the user."
        ),
    }


def _remember_pending_payment(kind: str, order_id: str, found: dict, presented: dict) -> None:
    """Keep enough to re-render the QR later.

    A PENDING_PAYMENT response is exactly when the user most needs the QR
    again - and at that moment the order may not be in the local log yet. So
    the intent string is stored separately, keyed by kind and by order id.
    """
    record = {
        "kind": kind,
        "order_id": order_id,
        "upi_uri": found.get("value") if found.get("kind") == "upi_uri" else None,
        # The https redirect Swiggy issued. Kept because it survives channels
        # that strip or refuse to linkify a upi:// scheme.
        "source_url": found.get("source_url") or (
            found.get("value") if found.get("kind") == "image_url" else None),
        "payment_link": presented.get("payment_link") or found.get("source_url"),
        "png": presented.get("png"),
        "payee": presented.get("payee"),
        "amount": presented.get("amount"),
        "expected_amount": presented.get("expected_amount"),
        "amount_verified": presented.get("amount_verified") is True,
    }
    store.set_pref(f"pending_payment_{kind}", record)
    store.set_pref("pending_payment_last", record)


def _log_order(kind: str, res, address_id: str | None) -> None:
    """Persist whatever we can identify about the order and its cost.

    Only writes a row when a real order id is found - a synthetic "unknown-*"
    id would double-count against the same order in `orders spend`.
    """
    text = response_blob(res)

    oid = order_id_in(text)
    if not oid:
        return

    m = _CART_TOTAL.search(text)
    amount = float(m.group(1)) if m else extract_payable(text)
    st = _STATUS.search(text)
    paas = _PAAS_ID.search(text)

    store.record_order(
        oid, kind, res,
        amount=amount,
        address_id=address_id,
        status=st.group(1) if st else None,
        coupon=None,
    )
    if paas:
        store.set_pref(f"last_paas_id_{kind}", paas.group(1))
        store.set_pref(f"last_order_id_{kind}", oid)

    # Always record: every order refreshes the learned preference profile.
    try:
        profile.learn()
    except Exception:  # noqa: BLE001
        pass


def wait_for_payment(kind: str, paas_id: str, order_id: str | None, addr: str,
                     timeout: float = 300, interval: float = 0,
                     auto_confirm: bool = True, on_tick=None) -> dict:
    """Drive a pending payment to a terminal state, then finalise it.

    This is the job Swiggy's payment widget does in a browser: watch the
    payment, and once it succeeds call confirm_order so the order actually
    goes through. Without it a CLI order stays PENDING even after the user has
    paid.
    """
    import time as _t

    server = _server_for(kind)
    deadline = _t.time() + timeout
    wait_for = interval if interval else MIN_POLL_INTERVAL
    polls, last = 0, None

    while _t.time() < deadline:
        polls += 1
        args = {"paasId": paas_id, "addressId": addr}
        if order_id:
            args["orderId"] = order_id
        last = call(server, "check_payment_status", args)
        blob = text_of(last)

        # Follow the cadence the server asks for, unless told otherwise.
        hinted = re.search(r'"pollIntervalSec"\s*:\s*(\d+)', blob)
        if hinted and not interval:
            wait_for = max(float(hinted.group(1)), MIN_POLL_INTERVAL)

        ok = '"isTerminalSuccess": true' in blob or '"status": "success"' in blob
        bad = '"isTerminalFailure": true' in blob or '"status": "failed"' in blob

        if ok:
            confirmed = '"confirmed": true' in blob or "CONFIRMED" in blob
            res = {"status": "paid", "polls": polls, "order_id": order_id,
                   "already_confirmed": confirmed, "detail": last}
            if not confirmed and auto_confirm and order_id:
                # The widget would do this; nothing else will.
                res["confirm"] = call(server, "confirm_order", {
                    "orderId": order_id, "paasId": paas_id, "addressId": addr})
                res["already_confirmed"] = True
            if order_id:
                _mark_order_status(order_id, "CONFIRMED")
            return res

        if bad:
            if order_id:
                _mark_order_status(order_id, "PAYMENT_FAILED")
            return {"status": "failed", "polls": polls, "detail": last}

        # Never sleep past the deadline: waiting 45s when 5s remain just
        # delays the timeout without adding a check.
        remaining = deadline - _t.time()
        if remaining <= 0:
            break
        if on_tick:
            on_tick(polls, round(remaining), wait_for)
        _t.sleep(min(wait_for, remaining))

    return {"status": "timeout", "polls": polls, "order_id": order_id, "detail": last}


def _mark_order_status(order_id: str, status: str) -> None:
    with store.connect() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


def _wait_after_order(kind: str, res, payload: dict, addr: str, timeout: int) -> None:
    """Take the payment through to confirmation, in place of the widget."""
    ids = store.get_pref(f"last_paas_id_{kind}"), store.get_pref(f"last_order_id_{kind}")
    paas, oid = ids
    if not paas:
        payload["wait"] = {"status": "no_paas_id",
                           "note": "No payment id came back; nothing to watch."}
        return
    err(f"\nWatching payment for order {oid} — pay now, this will confirm it.\n")

    def tick(n, remaining, every):
        err(f"  … still pending (check {n}, {remaining}s left, next in {every:.0f}s)")

    result = wait_for_payment(kind, paas, oid, addr, timeout=timeout, on_tick=tick)
    payload["wait"] = result
    if result["status"] == "paid":
        err("\n✅ Payment successful — order confirmed.\n")
    elif result["status"] == "failed":
        err("\n❌ Payment failed. The order was not placed.\n")
    else:
        err(f"\n⏳ Still pending after {timeout}s. Run `food pay wait` to keep watching.\n")
