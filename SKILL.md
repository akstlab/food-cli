---
name: food
description: Order restaurant food and groceries through Swiggy or Zepto with safe cart review, coupon selection, delivery-fee checks, explicit payment choice, and order tracking. Use when the user wants food or groceries delivered, wants to reorder, asks what is available nearby, asks about delivery fees, or wants to track an order.
---

# Food

Use the `food` CLI for Swiggy restaurants, Swiggy Instamart, and Zepto. Keep the
conversation short and spoken-friendly. Let the CLI’s safe defaults do the
work; run `food --help` or a subcommand’s `--help` when a less common option is
needed. See [REFERENCE.md](REFERENCE.md) only for deeper details.

## Hard rules

1. Never handle OTPs, UPI PINs, card details, or other payment credentials. Let
   the user enter or approve them in the provider or UPI app.
2. Never invent an address, item ID, restaurant ID, payment method, UPI app, or
   price. Use values returned by the CLI.
3. Never place an order until the user has heard and explicitly approved the
   vendor, items, delivery address, delivery fee, and exact payable total.
4. For a Swiggy restaurant or Instamart order, pass the exact approved total as
   `--max-total`. Never round it up or increase it after a price-change failure.
5. Never invent a UPI route. Let the CLI use generic UPI only when the live
   payment options advertise `PayWithQR`. Otherwise ask the user for one of the
   enabled apps; the CLI validates and saves that choice.
6. Never auto-accept a top-up, card-offer, high-fee, or price-change override.
   Explain the choice and wait for the user.
7. Never read internal IDs or local paths aloud. Attach QR images and send
   payment links instead.
8. Every placement is real. Treat `PENDING_PAYMENT` as a live unpaid order; do
   not place a duplicate.

## Good defaults

- Restaurant placement applies the best usable coupon by default. If the cart
  already has a coupon, it keeps it and does not probe again.
- Coupon calls verify item IDs and quantities. If Swiggy swaps the cart, the CLI
  restores the intended cart and stops so the user can review it again.
- The CLI automatically suggests worthwhile coupon top-ups. Ask whether the
  user wants the suggested items; never add them without permission.
- `restaurant cart` returns `checkout_preview` with `payable_total`,
  `delivery_fee`, `delivery_is_free`, `applied_coupon`, and a reconciled
  `bill_breakdown`. Require `orderability.orderable` not to be false.
- Placement fetches the live cart and delivery fee again immediately before
  ordering. It compares the provider’s placed total and delivery fee before
  exposing payment. A higher or unverifiable total blocks payment.
- Placement also re-checks the exact restaurant and proceeds only when its live
  delivery status is `OPEN`.
- For Swiggy UPI, provider-supported generic QR is the default. A chosen app is
  saved only for carts where generic QR is unavailable or the user requests it.
- Use `--wait` so the CLI watches and finalises a pending UPI payment.

## Restaurant golden path

Follow this order. Do not skip ahead.

### 1. Check sign-in and address

```bash
food auth status
food address search "home"
food address set-default <addressId> --label Home
```

If sign-in is needed, use `food auth serve --server food`. Ask the user which
returned address to use; do not choose one for them unless they previously set
a default.

### 2. Find and add what the user chose

```bash
food restaurant dish "thali" --veg --sort price --limit 8
food restaurant add --restaurant <restaurantId> --item <itemId>:1
```

Offer two or three useful choices, then wait. For variants or add-ons, inspect
`food restaurant add --help` and ask instead of guessing.
Only offer restaurants whose `availability_status` is `OPEN`; placement checks
the exact outlet again in case it closes before checkout.

### 3. Apply offers, then review the final cart

```bash
food restaurant best-offer --restaurant <restaurantId>
food restaurant cart
```

Apply the best coupon before quoting the total. If the CLI suggests a top-up,
tell the user what it adds, what it costs, and what it saves. Add it only after
they agree, then run `best-offer` and `cart` again.

Read the current items, address, delivery fee, taxes/charges, ETA, coupon, and
exact `checkout_preview.payable_total`. Treat `get_food_cart` as the only
pre-order bill source: coupon/update responses are not approval quotes,
payment options contain no bill, and order details exist only after placement.
Continue only when `bill_breakdown.complete` is true. If delivery is free, say
so explicitly. Stop on `orderability.orderable: false`, even if restaurant
search still says `OPEN`; the structured cart is the final availability veto.

### 4. Check payment options

```bash
food restaurant payment-options
```

If `generic_upi_qr` is present, no app choice is needed. Otherwise use a valid
saved preference or ask which enabled `upi_apps` entry the user wants. Never
pick the first app or fabricate a generic route.

### 5. Ask for final confirmation and place

After the user explicitly confirms the reviewed order:

```bash
food restaurant place -y --restaurant <restaurantId> \
  --max-total <exactApprovedTotal> --payment UPI --wait
```

Add `--intent-app "<chosen app name or exact id>"` only when the user chose an
app or the CLI asks for one. Use `food restaurant place --help` for other flags.

Read the CLI's JSON, not any MCP widget. For pending UPI, send both
`payment.qr_png` (as an attachment) and `payment.payment_link`; the flat
`payment` object always exposes both keys. If either value is missing, say
which artifact the provider omitted and do not place a duplicate. The user
must complete payment themselves.

## Payment options

- Swiggy restaurant: generic UPI QR when advertised; otherwise ask for an
  enabled app.
- Instamart: use generic UPI QR when advertised; otherwise ask for an enabled
  app. Never silently choose cash.
- Zepto: ask the user; supported flows include COD, online payment, wallet, and
  UPI Reserve Pay. Use `food zepto payment-options` for the live choices.

Never claim an option is available without reading the live payment-options
response.

## Groceries

For Instamart, search, add, then show `food im cart`. Continue only when
`checkout_preview.complete` is true. Read the delivery fee and exact
`payable_total`, explain high fees, and get explicit approval. Then run:

```bash
food im checkout -y --max-total <exactApprovedTotal> --payment UPI
```

The CLI uses generic QR only when the live options support it; otherwise it
asks for an app. For pending payment, send both `payment.qr_png` and
`payment.payment_link`. Use `food im checkout --help` for less common options.

For Zepto, select an address first, run `food zepto usual`, search, add, show
the cart, ask for a payment method, then use `food zepto place --help`.

## Handling gates

- `blocked_upi_app_choice`: show enabled apps and ask the user.
- `blocked_restaurant_not_open` or `blocked_unverified_restaurant_status`:
  search again and choose an exact outlet whose status is `OPEN`.
- `blocked_cart_unavailable`: the authoritative cart says the restaurant or an
  item cannot be ordered; choose a new restaurant/item and review again.
- `blocked_high_fees`, `blocked_card_offers`, or `blocked_near_misses`: explain
  the tradeoff and wait; never add an override yourself.
- `blocked_cart_recovered`: show `restaurant cart` again and request fresh
  approval.
- `blocked_incomplete_bill_breakdown`: do not place or infer missing fees; show
  the cart again and wait for a complete reconciled bill.
- `blocked_total_changed` or any unverified-total status: do not pay. Explain
  the delivery-fee change only when `delivery_fee.verified` is true. If the
  placed fee is absent, say the cause is unknown; never infer a delivery,
  Swift, tax, or other hidden fee. Show the fresh cart and ask again.
- `PENDING_PAYMENT`: send the existing QR/link or use `food pay qr`; never
  create another order.
- Payment timeout: report that it is still processing; do not call it failed.

## After ordering

Use `food orders track <orderId>` for status. If the user has an expense or
nutrition tracker, log the real charged amount and consumed items; label
nutrition values as estimates.

Keep user-facing summaries free of JSON, IDs, URLs, and file paths.
