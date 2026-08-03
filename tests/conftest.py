"""Shared fixtures.

Every test runs against a temporary SQLite file and a fake MCP layer — no
network, no real account, no personal data. All sample data below is invented.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

# Point the store at a throwaway DB before anything imports it.
_TMPDIR = tempfile.mkdtemp(prefix="swiggy-cli-tests-")
os.environ["FOOD_CLI_DB"] = os.path.join(_TMPDIR, "test.db")

from food_cli.core import migrations, store  # noqa: E402


ADDRESS_ID = "addr_test_001"

FAKE_ADDRESSES = (
    "Found 3 saved addresses (page 1 of 1, showing 3):\n"
    "1. [Home] Test User: 10 Baker Street, Springfield, 400001, India (ID: addr_test_001)\n"
    "2. [Work] Test User: 42 Industrial Way, Shelbyville, 400002, India (ID: addr_test_002)\n"
    "3. [Gym] Test User: 7 Seaside Road, Ogdenville, 400003, India (ID: addr_test_003)\n"
)

FAKE_RESTAURANTS = (
    'Found 4 restaurants for "pizza":\n'
    "1. Vesuvio Pizzeria (Ad) — Italian, Continental | 4.6★ | 22 min | ₹500 for two | OPEN (ID: 9001)\n"
    "2. Nordic Kitchen — Scandinavian, Bakery | 4.3★ | 35 min | ₹700 for two | OPEN (ID: 9002)\n"
    "3. Sakura Ramen House — Japanese, Noodles | 4.8★ | 48 min | ₹900 for two | OPEN (ID: 9003)\n"
    "4. Mystery Dish Row —  | undefined★ | ? min |  (ID: 9004)\n"
)

FAKE_DISHES = (
    'Found 3 menu items for "platter":\n'
    "1. Garden Platter — ₹240 | Veg | 4.5★ | Vesuvio Pizzeria (restaurantId: 9001) (ID: it_100)\n"
    "2. Smoked Salmon Plate — ₹520 | Non-veg | 4.7★ | Nordic Kitchen (restaurantId: 9002) (ID: it_200)\n"
    "3. Sharing Platter (For 2-3 People) — ₹800 | Veg | 4.4★ | Sakura Ramen House "
    "(restaurantId: 9003) (ID: it_300)\n"
)

FAKE_MENU = (
    "Menu for Vesuvio Pizzeria (ID: 9001) [image: https://cdn.example.com/rest.jpg]\n"
    "Page 1 — 2 of 2 categories.\n"
    "## Recommended\n"
    "  - Garden Platter — ₹240 | Veg, Bestseller [image: https://cdn.example.com/a.jpg] (ID: it_100)\n"
    "  - Truffle Fries — ₹120 | Veg [image: https://cdn.example.com/b.jpg] (ID: it_101)\n"
    "  - Lemon Soda — ₹60 | Veg (ID: it_102)\n"
    "  - Ketchup Sachet — ₹1 | Veg (ID: it_103)\n"
    "  - Garlic Bread — ₹80 | Veg, Bestseller (ID: it_104)\n"
    "  - Grilled Chicken Wings — ₹200 | Non-veg (ID: it_105)\n"
)

FAKE_CART = (
    "Items (2):\n"
    "  - Garden Platter — ₹240 (ID: it_100)\n"
    "  - Truffle Fries — ₹120 (ID: it_101)\n"
    "\nItem total: ₹360\nDelivery: FREE\nTaxes & charges: ₹70\nTO PAY: ₹430\n"
)

FAKE_FOOD_CART_DATA = {
    "items": [
        {"menu_item_id": "it_100", "name": "Garden Platter", "quantity": 1},
        {"menu_item_id": "it_101", "name": "Truffle Fries", "quantity": 1},
    ],
    "pricing": {
        "item_total": 360,
        "delivery_charge": 0,
        "taxes_and_charges": 70,
        "to_pay": 430,
    },
    "offers": {"coupon_applied": None, "coupon_discount": 0},
}

FAKE_COUPONS = (
    "Found 4 coupons (1 applicable):\n"
    "**Great deal**\n"
    "  - WELCOME50 [✅ APPLICABLE] — Flat ₹50 off (code: c-0001)\n"
    "  - NEARLY [❌ NOT APPLICABLE] — Add ₹60 more to get a discount upto ₹90 (code: c-0002)\n"
    "  - FLAT300 [❌ NOT APPLICABLE] — Add ₹2000 more to avail this offer (code: c-0003)\n"
    "  - BANKX [✅ APPLICABLE] — 10% off up to ₹150 on HDFC Credit Card (code: c-0004)\n"
    "\nTo apply a coupon, use apply_food_coupon with the coupon code and addressId.\n"
)

FAKE_ORDERS = (
    "Found 2 orders:\n"
    "1. Order 5550001 — Vesuvio Pizzeria | March 3, 8:00 PM | Delivered | ₹430 "
    "[reorderable] — Garden Platter (1),Truffle Fries (2)\n"
    "2. Order 5550002 — Nordic Kitchen | March 1, 1:00 PM | Delivered | ₹700 "
    "[reorderable] — Smoked Salmon Plate (1)\n"
)

FAKE_IM_ORDERS = {
    "orders": [
        {
            "orderId": "7770001",
            "status": "DELIVERED",
            "createdAt": "2026-03-02T10:00:00.000Z",
            "totalAmount": 350,
            "items": [{"name": "Oat Milk", "quantity": 2}, {"name": "Rye Bread", "quantity": 1}],
        }
    ]
}

FAKE_PAYMENT_OPTIONS = [
    "Found 2 UPI option(s) + Cash on Delivery for this cart (₹430).",
    {
        "platforms": {"desktop": {"groupName": "UPI", "methods": [{"id": "PayWithQR"}]}},
        "allMethods": [
            {"id": "gpay://upi/", "groupName": "UPI", "displayName": "Google Pay", "enabled": True},
            {"id": "cod", "groupName": "COD", "displayName": "Pay on delivery", "enabled": True},
        ],
    },
]

FAKE_PLACE_ORDER = [
    "UPI payment initiated. Scan the QR code to pay.",
    {
        "orderId": "8880001",
        "paasId": "ppp-0001",
        "cartTotal": 430,
        "status": "PENDING_PAYMENT",
        "upiIntentUrl": "upi://pay?pa=merchant@bank&am=430.00&cu=INR",
        "bridgeUrl": "https://mcp.example.com/deeplink-redirect?link=zzz&mode=qr",
    },
]

FAKE_IM_CART = {
    "success": True,
    "data": {
        "cartTotalAmount": "₹228",
        "items": [
            {"spinId": "sp_1", "itemName": "Oat Milk", "quantity": 1, "discountedFinalPrice": 120},
            {"spinId": "sp_2", "itemName": "Rye Bread", "quantity": 1, "discountedFinalPrice": 96},
        ],
        "billBreakdown": {
            "lineItems": [
                {"label": "Item total", "value": "₹216"},
                {"label": "Delivery fee", "value": "₹7"},
                {"label": "Handling fee", "value": "₹5"},
            ],
            "toPay": {"label": "To Pay", "value": "₹228"},
        },
    },
}

FAKE_IM_CHECKOUT = [
    "Instamart payment initiated. Scan the QR code to pay.",
    {
        "orderId": "8880001",
        "paasId": "ppp-0001",
        "cartTotal": 228,
        "status": "PENDING_PAYMENT",
        "upiIntentUrl": "upi://pay?pa=merchant@bank&am=228.00&cu=INR",
        "bridgeUrl": "https://mcp.example.com/deeplink-redirect?link=im-test&mode=qr",
    },
]


# Zepto returns addresses as prose and repeats the ids in a trailing block.
# Every value below is invented.
FAKE_ZEPTO_ADDRESSES = (
    "Found 2 saved address(es):\n"
    "\n"
    "1. Home: 10 Baker Street, Springfield, 400001, India\n"
    "2. Office: 42 Industrial Way, Shelbyville, 400002, India\n"
    "\n"
    "---\n"
    "[SYSTEM NOTE: The following address IDs are for your internal use.]\n"
    "\n"
    "Address IDs:\n"
    '1. "Home" → ID: 11111111-2222-3333-4444-555555555555\n'
    '2. "Office" → ID: 66666666-7777-8888-9999-000000000000\n'
)

ZEPTO_ADDRESS_ID = "11111111-2222-3333-4444-555555555555"

FAKE_ZEPTO_PRODUCTS = (
    "Found 2 products for 'oat milk':\n"
    "1. Barista Oat Drink 1L — ₹120 (productVariantId: pv_001, storeProductId: sp_001)\n"
    "2. Creamy Oat Drink 500ml — ₹70 (productVariantId: pv_002, storeProductId: sp_002)\n"
)

FAKE_ZEPTO_CART = {
    "items": [
        {"productVariantId": "pv_001", "storeProductId": "sp_001",
         "name": "Barista Oat Drink 1L", "quantity": 2, "price": 120},
    ],
    "cartTotal": 240,
}

FAKE_ZEPTO_PAYMENT_METHODS = (
    "Available payment methods for this cart (₹240):\n"
    "- COD (Cash on Delivery): available\n"
    "- Online payment: available\n"
    "- Zepto Cash: insufficient balance\n"
)

FAKE_ZEPTO_ORDER = {
    "orderId": "99900011122",
    "status": "CONFIRMED",
    "paymentMode": "COD",
    "cartTotal": 240,
}

FAKE_ZEPTO_PAST_ITEMS = (
    "Products ordered in the last 30 orders:\n"
    "1. Barista Oat Drink 1L (productVariantId: pv_001) — 8 orders\n"
    "2. Rye Loaf 400g (productVariantId: pv_003) — 5 orders\n"
)


def _wrap(content):
    return {"isError": False, "content": content}


DEFAULT_RESPONSES = {
    ("food", "get_addresses"): _wrap(FAKE_ADDRESSES),
    ("food", "search_restaurants"): _wrap(FAKE_RESTAURANTS),
    ("food", "search_menu"): _wrap(FAKE_DISHES),
    ("food", "get_restaurant_menu"): _wrap(FAKE_MENU),
    ("food", "get_food_cart"): {
        "isError": False, "content": FAKE_CART,
        "structuredContent": {"data": FAKE_FOOD_CART_DATA},
    },
    ("food", "update_food_cart"): _wrap(FAKE_CART),
    ("food", "flush_food_cart"): _wrap("Flushed Food cart successfully"),
    ("food", "fetch_food_coupons"): _wrap(FAKE_COUPONS),
    ("food", "apply_food_coupon"): _wrap("Coupon is not eligible on the items in your cart."),
    ("food", "get_payment_options"): _wrap(FAKE_PAYMENT_OPTIONS),
    ("food", "place_food_order"): _wrap(FAKE_PLACE_ORDER),
    ("food", "get_food_orders"): _wrap(FAKE_ORDERS),
    ("food", "get_food_delivery_status"): _wrap("etaText: 12 mins"),
    ("food", "check_payment_status"): _wrap(
        ["✅ Payment SUCCESS", {"status": "success", "isTerminalSuccess": True,
                               "confirmed": True}]
    ),
    ("food", "confirm_order"): _wrap({"orderId": "8880001", "status": "CONFIRMED"}),
    ("instamart", "get_addresses"): _wrap(FAKE_ADDRESSES),
    ("instamart", "search_products"): _wrap({"data": {"products": []}}),
    ("instamart", "your_go_to_items"): _wrap({"data": {"products": []}}),
    ("instamart", "get_cart"): _wrap(FAKE_IM_CART),
    ("instamart", "update_cart"): _wrap(FAKE_IM_CART),
    ("instamart", "clear_cart"): _wrap("cleared"),
    ("instamart", "checkout"): _wrap(FAKE_IM_CHECKOUT),
    ("instamart", "get_orders"): _wrap([FAKE_IM_ORDERS]),
    ("instamart", "get_payment_options"): _wrap(FAKE_PAYMENT_OPTIONS),
    ("instamart", "get_delivery_status"): _wrap({"etaText": "9 mins"}),
    ("instamart", "check_payment_status"): _wrap(
        ["✅ Payment SUCCESS", {"status": "success", "isTerminalSuccess": True, "confirmed": True}]
    ),
    ("instamart", "confirm_order"): _wrap({"orderId": "8880001", "status": "CONFIRMED"}),
    ("zepto", "list_saved_addresses"): _wrap(FAKE_ZEPTO_ADDRESSES),
    ("zepto", "select_saved_address"): _wrap(
        "✅ Address selected: Home\nStore ID: store_abc\nYou can now search."
    ),
    ("zepto", "get_user_details"): _wrap({"isRegistered": True, "userType": "consumer"}),
    ("zepto", "search_products"): _wrap(FAKE_ZEPTO_PRODUCTS),
    ("zepto", "search_multiple_products"): _wrap(FAKE_ZEPTO_PRODUCTS),
    ("zepto", "get_product_details"): _wrap({"productVariantId": "pv_001", "price": 120}),
    ("zepto", "get_past_order_items"): _wrap(FAKE_ZEPTO_PAST_ITEMS),
    ("zepto", "view_cart"): _wrap(FAKE_ZEPTO_CART),
    ("zepto", "update_cart"): _wrap(FAKE_ZEPTO_CART),
    ("zepto", "get_payment_methods"): _wrap(FAKE_ZEPTO_PAYMENT_METHODS),
    ("zepto", "create_order"): _wrap(FAKE_ZEPTO_ORDER),
    ("zepto", "create_online_payment_order"): _wrap(
        {**FAKE_ZEPTO_ORDER, "status": "PENDING_PAYMENT",
         "paymentLink": "https://pay.example.com/z/99900011122"}
    ),
    ("zepto", "create_wallet_order"): _wrap(FAKE_ZEPTO_ORDER),
    ("zepto", "create_upi_reserve_pay_order"): _wrap(FAKE_ZEPTO_ORDER),
    ("zepto", "check_payment_status"): _wrap({"orderId": "99900011122", "status": "SUCCESS"}),
    ("zepto", "list_order_history"): _wrap({"orders": [FAKE_ZEPTO_ORDER]}),
    ("zepto", "get_order_detail"): _wrap(FAKE_ZEPTO_ORDER),
    ("zepto", "get_location_serviceability"): _wrap({"serviceable": True, "storeId": "store_abc"}),
}


class FakeMCP:
    """Records calls and returns canned responses. No network."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.responses = dict(DEFAULT_RESPONSES)

    def set(self, server, tool, content, is_error=False):
        self.responses[(server, tool)] = {"isError": is_error, "content": content}

    def __call__(self, server, tool, args):
        self.calls.append((server, tool, args))
        try:
            return self.responses[(server, tool)]
        except KeyError:
            raise AssertionError(f"unmocked MCP call: {server}.{tool}") from None

    def tools_called(self):
        return [t for _, t, _ in self.calls]


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Isolated SQLite file per test, migrated to head."""
    db = tmp_path / "food.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    # The upgrade memo is keyed by path, but clearing it keeps each test honest
    # about running the migrations rather than inheriting another test's state.
    migrations.reset_memo()
    return db


@pytest.fixture()
def mcp(monkeypatch, fresh_db):
    """Replace the MCP transport with a fake, for every caller at once.

    Patching is done at `client.call_tool` - the single async boundary every
    command reaches through `commands.common.call`. Patching the `call` helper
    instead would only catch modules that had not already imported it by name,
    which is how a command can silently keep talking to the network in a test.
    """
    from food_cli.mcp import client

    fake = FakeMCP()

    async def fake_call_tool(server, tool, args, on_consent_url=None):
        return fake(server, tool, args)

    async def fake_list_tools(server, on_consent_url=None):
        return [{"name": "x", "description": "d", "input_schema": {}, "output_schema": None}]

    monkeypatch.setattr(client, "call_tool", fake_call_tool)
    monkeypatch.setattr(client, "list_tools", fake_list_tools)

    store.set_pref("default_address_id", ADDRESS_ID)
    return fake


@pytest.fixture()
def runner():
    from typer.testing import CliRunner
    return CliRunner()


def parse_out(result):
    """Parse the JSON a command wrote to stdout."""
    text = result.stdout
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    assert start != -1, f"no JSON in output: {text[:400]}"
    return json.loads(text[start:])


# ---------------------------------------------------------------- DNS stub

# The security layer resolves hostnames to decide whether a URL is safe. Tests
# must never depend on the machine's actual resolver: a host that is offline,
# behind a captive portal, or running a faulty stub resolver would otherwise
# fail the suite for reasons unrelated to this code. (Observed in the wild: a
# resolver returning EBUSY rather than NXDOMAIN for a reserved .invalid name.)
#
# So resolution is stubbed deterministically for every test. Tests that care
# about resolver *failure* patch it themselves.

from food_cli.core import security as _security  # noqa: E402

# The genuine implementations, captured before any stubbing, so a test can put
# them back when it is those functions themselves under test.
REAL_RESOLVED_IPS = _security._resolved_ips
REAL_SAFE_GET = _security.safe_get

_PUBLIC_IP = "93.184.216.34"
_FAKE_DNS = {
    "example.com": _PUBLIC_IP,
    "cdn.example.com": _PUBLIC_IP,
    "mcp.example.com": _PUBLIC_IP,
    "ok": _PUBLIC_IP,
    "localhost": "127.0.0.1",
}


@pytest.fixture(autouse=True)
def deterministic_dns(monkeypatch, request):
    """Resolve hostnames from a fixed table instead of the real resolver."""
    import ipaddress
    import socket as _socket

    from food_cli.core import security

    def fake_resolve(host: str):
        # Literal addresses resolve to themselves - this is what makes the
        # loopback / link-local / private-range checks testable offline.
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        if host in _FAKE_DNS:
            return [ipaddress.ip_address(_FAKE_DNS[host])]
        raise security.UnsafeURLError(f"cannot resolve host {host!r} (stubbed)")

    monkeypatch.setattr(security, "_resolved_ips", fake_resolve)

    # Guard against a test reaching the real resolver by accident, while still
    # allowing loopback: the OAuth callback tests genuinely serve and fetch
    # over 127.0.0.1, and that must keep working offline.
    _real_getaddrinfo = _socket.getaddrinfo

    def guarded(host, *a, **k):
        if host in (None, "", "localhost", "127.0.0.1", "::1"):
            return _real_getaddrinfo(host, *a, **k)
        try:
            ipaddress.ip_address(str(host).strip("[]"))
        except ValueError:
            raise AssertionError(
                f"test tried to resolve {host!r} via real DNS - stub it instead"
            ) from None
        return _real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(_socket, "getaddrinfo", guarded)

    # No test may perform a real HTTP fetch either. Previously these calls went
    # out to the network and merely happened to fail, which hid the dependency.
    # Tests that exercise safe_get itself opt out with @pytest.mark.real_fetch.
    if "real_fetch" not in request.keywords:
        monkeypatch.setattr(security, "safe_get", lambda *a, **k: None)


def loopback_get(url: str) -> str:
    """Fetch a loopback URL over a raw socket.

    urllib/http.client call getaddrinfo even for a literal address, so a host
    with a broken resolver cannot run these tests. Connecting to an (ip, port)
    tuple directly skips resolution entirely and keeps the suite hermetic.
    """
    import socket as _socket
    from urllib.parse import urlparse

    u = urlparse(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect((u.hostname, u.port))
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    return raw.split("\r\n\r\n", 1)[-1]


def patch_call(monkeypatch, fn):
    """Route every command's MCP call through `fn(server, tool, args)`.

    Commands import `call` by name, so replacing `commands.call` would not
    reach them. This substitutes the async transport underneath instead, which
    every path genuinely shares.
    """
    from food_cli.mcp import client

    async def _call_tool(server, tool, args, on_consent_url=None):
        return fn(server, tool, args)

    monkeypatch.setattr(client, "call_tool", _call_tool)
