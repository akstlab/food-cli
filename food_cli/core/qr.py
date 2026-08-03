"""Selected-app UPI payment handling.

The payload is the intent for the app the user selected, either returned
directly or extracted from Swiggy's payment page. The app scheme is preserved.
From that string we can render a QR and read out the UPI id and amount.

We deliberately do not chase bitmaps Swiggy may embed instead. A QR image
carries no payee or amount we can verify or speak, so it is a worse artefact
and a second code path for no gain. If there is no intent, we say so.

The intent is surfaced as text, ASCII in the terminal, and an owner-only PNG.
The user always authorises the payment themselves; the CLI never handles it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import webbrowser
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote

import segno

from . import paths, security

QR_DIR = paths.subdir("qr")
# Images we downloaded ourselves are also safe to open.
MEDIA_ROOT = paths.subdir("media")

_UPI_URI = re.compile(r"upi://[^\s\"'\\<>]+", re.I)
# Swiggy's payment link redirects into the exact app the user selected. Keep
# that scheme intact: choosing Paytm must remain Paytm rather than silently
# becoming a generic UPI route.
_APP_INTENT = re.compile(
    r"\b(?:gpay|tez|phonepe|paytmmp|paytm|bhim|credpay|super)://[^\s\"'\\<>]+",
    re.I,
)
_JS_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def _decode_layer(value: str) -> str:
    """Decode one safe HTML/URL/JavaScript escaping layer."""
    value = unescape(value).replace(r"\/", "/")

    def repl(match: re.Match) -> str:
        raw = match.group(1) or match.group(2)
        return chr(int(raw, 16))

    return unquote(_JS_UNICODE_ESCAPE.sub(repl, value))


def normalise_intent(text: str) -> str | None:
    """Extract a payment intent without changing its selected app scheme.

    Paytm redirects often percent-encode either its app deeplink or a nested
    UPI URI. Decode a bounded number of layers to find it, but preserve the
    outer Paytm intent when present.
    """
    candidate = text or ""
    for _ in range(5):
        previous = candidate
        decoded = _decode_layer(candidate)
        if decoded != candidate:
            candidate = decoded
        m = _APP_INTENT.search(candidate)
        if m and ("pa=" in m.group(0) or "upi" in m.group(0).lower()):
            return m.group(0).rstrip("),.;]}")
        m = _UPI_URI.search(candidate)
        if m:
            return m.group(0).rstrip("),.;]}")
        if candidate == previous:
            break
    return None


class _PaymentHTMLParser(HTMLParser):
    """Collect URI-bearing values without depending on Swiggy's DOM layout."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and (name in {"href", "src", "content", "value"}
                          or name.startswith("data-")):
                self.candidates.append(value)

    def handle_data(self, data: str) -> None:
        if "://" in data or "%3A%2F%2F" in data.upper() or r"\u003a" in data.lower():
            self.candidates.append(data)


def intent_from_html(page: str) -> str | None:
    """Extract a UPI/app intent from HTML attributes, scripts, or text."""
    parser = _PaymentHTMLParser()
    try:
        parser.feed(page or "")
    except Exception:  # malformed provider HTML still gets a raw-text fallback
        pass
    for candidate in [*parser.candidates, page or ""]:
        intent = normalise_intent(candidate)
        if intent:
            return intent
    return None


_INTENT_FIELDS = {
    "upiintenturl", "upiuri", "upi_uri", "appintent", "app_intent",
    "paymentintent", "payment_intent", "deeplink",
}


def _named_values(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).casefold(), nested
            yield from _named_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _named_values(nested)


def intent_from_payload(payload) -> str | None:
    """Prefer the confirmation widget's structured payment fields."""
    for key, value in _named_values(payload):
        if key not in _INTENT_FIELDS or not isinstance(value, str):
            continue
        intent = normalise_intent(value)
        if intent:
            return intent
    blob = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return normalise_intent(blob)
_IMG_URL = re.compile(r"https?://[^\s\"'\\<>]*(?:qr|QR)[^\s\"'\\<>]*", re.I)


_BRIDGE = re.compile(r"https://[^\s\"'\\<>]*deeplink-redirect[^\s\"'\\<>]*", re.I)
# The id may be plain, quoted, or JSON-escaped (\" ) depending on whether it
# came from the prose block or a serialised payload.
_ORDER_ID = re.compile(r'\\?"?orderId\\?"?\s*:\s*\\?"?([A-Za-z0-9\-]{6,})', re.I)


def extract_order_id(payload) -> str | None:
    blob = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    m = _ORDER_ID.search(blob)
    return m.group(1) if m else None


def resolve_payment_page(url: str) -> dict:
    """Pull the `upi://` intent out of Swiggy's 'Scan to pay' page.

    Swiggy renders the QR in its own widget and hands back no image asset, so
    the intent embedded in that page is the only transferable payload - and the
    best one, since it yields both a QR we can render and the UPI id to read
    out. If it is not there we return nothing rather than substituting a bitmap
    that carries no payee or amount.
    """
    body = security.safe_get(url, timeout=25)
    if not body:
        return {}
    try:
        page = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    intent = intent_from_html(page)
    if not intent:
        return {}
    return {"upi_uri": intent, "source_url": url}


def _resolve_bridge(url: str) -> str | None:
    return resolve_payment_page(url).get("upi_uri")


def find_qr(payload) -> dict | None:
    """Locate a UPI QR anywhere in a tool response."""
    blob = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    direct = intent_from_payload(payload)
    if direct:
        return {"kind": "upi_uri", "value": direct}
    m = _BRIDGE.search(blob)
    if m:
        intent = resolve_payment_page(m.group(0)).get("upi_uri")
        if intent:
            return {"kind": "upi_uri", "value": intent, "source_url": m.group(0)}
        return {"kind": "image_url", "value": m.group(0)}
    m = _IMG_URL.search(blob)
    if m:
        return {"kind": "image_url", "value": m.group(0)}
    return None


def _param(uri: str, key: str) -> str | None:
    decoded = uri
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    m = re.search(rf"[?&]{key}=([^&\s]+)", decoded)
    return m.group(1) if m else None


def _amount_of(uri: str) -> float | None:
    v = _param(uri, "am")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _payee_of(uri: str) -> str | None:
    return _param(uri, "pa")


def _open(path_or_url: str) -> bool:
    """Hand a target to the OS viewer — but only if it is safe to.

    `open` on macOS dispatches on scheme, so an attacker-supplied `file://` or
    a custom app scheme would be launched verbatim. Values here can originate
    in a remote tool response, so only https URLs and files under our own
    directories are ever opened.
    """
    if not security.is_openable(path_or_url, [QR_DIR, MEDIA_ROOT]):
        print(f"[food-cli] refusing to open untrusted target: {path_or_url[:80]}",
              file=sys.stderr)
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path_or_url], check=False)
            return True
        return webbrowser.open(path_or_url)
    except Exception:  # noqa: BLE001
        return False


def present(qr: dict, order_ref: str = "order", open_browser: bool = True) -> dict:
    """Render the QR for the user. Returns where it ended up."""
    # QR codes encode a live payment intent - keep them owner-only.
    security.secure_dir(QR_DIR)
    result: dict = {"kind": qr["kind"]}

    if qr["kind"] == "upi_uri":
        code = segno.make(qr["value"], error="m")
        # ASCII straight to the terminal - scannable off the screen.
        print("\nScan this with the selected UPI app to pay:\n", file=sys.stderr)
        code.terminal(out=sys.stderr, border=2)

        png = QR_DIR / f"{order_ref}.png"
        code.save(str(png), scale=8, border=2)
        svg = QR_DIR / f"{order_ref}.svg"
        code.save(str(svg), scale=8, border=2)
        for f in (png, svg):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        result["png"] = str(png)
        result["svg"] = str(svg)
        result["upi_uri"] = qr["value"]
        result["amount"] = _amount_of(qr["value"])
        result["payee"] = _payee_of(qr["value"])
        if open_browser:
            result["opened"] = _open(str(png))

    else:  # image_url - Swiggy gave a page, not an intent
        result["url"] = qr["value"]
        result["note"] = (
            "No UPI intent in Swiggy's payment page, so there is nothing to "
            "attach. Open the URL in a browser to scan it, or use Cash on "
            "delivery where available."
        )
        if open_browser:
            result["opened"] = _open(qr["value"])

    # Surface the UPI id and amount in text too: scanning is not always
    # possible (no camera, screen reader, paying from another device), and
    # these can be typed into the selected UPI app by hand.
    payee = result.get("payee")
    amount = result.get("amount")
    if payee:
        print(
            f"\nOr pay by hand in the selected UPI app:\n"
            f"    UPI ID : {payee}\n"
            + (f"    Amount : \u20b9{amount:.2f}\n" if amount else ""),
            file=sys.stderr,
        )
    print(
        "\n>>> Complete the payment yourself in your UPI app. "
        "Never share the OTP or UPI PIN with anyone, including this tool.\n",
        file=sys.stderr,
    )
    return result
