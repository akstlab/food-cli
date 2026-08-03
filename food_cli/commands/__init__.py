"""CLI command groups.

Each module owns one group and its Typer app. This package re-exports the
parsing and pricing helpers that are useful on their own - they are pure
functions over provider responses, and are worth calling directly from a script
or a test without going through the CLI.

Note for tests: patching `commands.call` here does NOT redirect the modules,
because they imported the name at import time. Substitute the transport at
`food_cli.mcp.client.call_tool`, which is the single boundary everything
funnels through.
"""

from ..core import media, profile, qr, store  # noqa: F401  (re-exported)
from .checkout import (  # noqa: F401
    _log_order,
    _mark_order_status,
    _payment_block,
    _remember_pending_payment,
    _wait_after_order,
    order_id_in,
    wait_for_payment,
)
from .common import (  # noqa: F401
    DEFAULT_FREE_DELIVERY_THRESHOLD,
    FEE_RATIO_WARN,
    MIN_POLL_INTERVAL,
    NEAR_MISS_MAX_RATIO,
    PAYMENT_REQUIRED_HINT,
    _no_open_default,
    _rupees,
    call,
    err,
    extract_payable,
    extract_delivery_fee,
    extract_taxes_and_charges,
    extract_total,
    fee_analysis,
    out,
    resolve_address,
    response_blob,
    text_of,
    upstream_error,
)
from .food import (  # noqa: F401
    _cart_items,
    _menu_items_for,
    _serves_count,
    apply_best_coupon,
    cart_orderability,
    parse_bill_breakdown,
    parse_dishes,
    parse_restaurants,
    restaurant_availability_in,
    topup_upside,
)
from .instamart import _instamart_fee_analysis  # noqa: F401
from .orders import parse_instamart_orders, parse_order_history  # noqa: F401
from .zepto import parse_addresses as parse_zepto_addresses  # noqa: F401
