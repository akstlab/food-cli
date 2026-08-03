# Reference

Detail that would bloat [SKILL.md](SKILL.md). Read the parts you need.

## Contents

- [Layout](#layout)
- [Providers](#providers)
- [Addons and variants](#addons-and-variants)
- [Why the offer engine probes](#why-the-offer-engine-probes)
- [The payment widget, and why the CLI polls](#the-payment-widget-and-why-the-cli-polls)
- [Delivery fees](#delivery-fees)
- [Zepto specifics](#zepto-specifics)
- [Preferences: learned vs stated](#preferences-learned-vs-stated)
- [Logging to expense and calorie trackers](#logging-to-expense-and-calorie-trackers)
- [Order history and analytics](#order-history-and-analytics)
- [Images](#images)
- [Payment findings](#payment-findings)
- [Storage and migrations](#storage-and-migrations)
- [Full command list](#full-command-list)

---

## Layout

```
food_cli/
  cli.py            assembles the Typer app; no logic
  commands/         one module per group, plus:
    common.py       out/err, `call`, address resolution, money parsing
    checkout.py     order + payment plumbing shared by every provider
  providers/        one module per provider, plus the registry
    base.py         Provider / Server / OAuthConfig
    roles.py        capability -> real tool name
  mcp/              transport: client, oauth, loopback callback
  core/             store, security, qr, media, profile, paths, migrations
  offers/           coupon selection and top-up planning
  migrations/       Alembic revisions
```

Everything that talks to a server goes through `commands.common.call`, which is
the single boundary the test suite replaces.

## Providers

A *provider* is a company; a *server* is one MCP endpoint it exposes. Swiggy has
two servers behind one token; Zepto has one.

```bash
food mcp providers            # endpoints, auth style, which servers exist
food auth whoami              # who is signed in, and what each token covers
```

Adding a provider is one module plus a registry entry. Two things differ between
them and are configured, not assumed:

- **`callback_path`** — providers whitelist redirect URIs by exact path. Swiggy
  accepts `/oauth/callback`; Zepto whitelists `/callback`.
- **`resource`** — RFC 8707. Zepto requires the token to be bound to
  `https://mcp.zepto.co.in`; Swiggy does not use it.

A token is only ever written to servers of the **same** provider. Swiggy's one
token legitimately covers food and Instamart; writing it into Zepto's row would
be silent corruption, so `sibling_servers` gates it.

### Two findings from the live servers

- **The redirect host must be spelled `localhost`.** Zepto's edge (AWS ELB)
  returns a bare `403` for any request containing a literal `127.0.0.1` — both
  client registration and authorization fail, with no useful body. The exact
  same request with `localhost` succeeds. The listener still binds `127.0.0.1`
  only; only the spelling changes.
- **A wrong-audience token looks like a hang.** Without `resource`, Zepto issues
  a token that authenticates and then fails every tool call with *"The token is
  not intended for this resource"*. The MCP client responds by starting a fresh
  browser authorization, so the symptom is a re-authorization loop, not an
  error. It cannot be repaired by refreshing (`invalid_grant: Resource
  mismatch`) — the grant itself is bound to the wrong audience.

### Tool-name discovery

Swiggy and Zepto both publish their tool names, so `providers/roles.py` states
them outright in `KNOWN`. For any server that does not, roles are resolved by
listing its tools and scoring names and descriptions, cached in SQLite.

A role that cannot be matched confidently resolves to `None` and the command
says so, rather than calling a guessed tool that might spend money.

```bash
food mcp roles zepto              # what backs each capability
food mcp roles zepto --refresh    # re-discover after a provider changes
```

## Addons and variants

Swiggy distinguishes two things:

- **Variants** — a required choice that defines the item: size, crust, base.
  Adding an item that has variations *without* one may fail or silently take a
  default the user did not pick.
- **Addons** — optional extras: dips, toppings, an upgrade.

Both are `groupId` + `choiceId`/`variationId` pairs found in the restaurant menu.

```bash
food restaurant add --restaurant 9001 --item it_100:1 \
    --variant it_100:g1:v9:Large \
    --addon   it_100:g4:c7:Extra_cheese:30

food restaurant edit --item it_100 --qty 2 --clear-addons
```

The optional `:name:price` suffixes are for readability only; ids are what count.

### The rewrite caveat

Swiggy has no partial cart update — `update_food_cart` replaces everything. So
`edit`, `remove` and `set-qty` read the cart, change one thing and write it all
back. But the cart read-back is prose and **does not include addon ids**.

The CLI therefore remembers what it sent (`cart_items:<restaurantId>` in the
local store) and replays it. Consequence:

- Items added **through this CLI** keep their addons across edits.
- Items added **in the Swiggy app** are opaque to us and their addons **can be
  lost** on a rewrite.

If the user built the cart in the app, show `restaurant cart` and confirm rather
than editing.

## Chains with several outlets

A chain lists every nearby outlet, and they are indistinguishable in a spoken
summary — "McDonald's" twice. The farther one is strictly worse: identical food,
longer wait, usually a bigger delivery fee. Being in the results already means
the outlet stocks the dish, since a search only returns restaurants that have
it, so distance is the whole decision.

`brand_of()` folds an outlet name to its chain by cutting at the first `,`,
`-`, `(` or `|` and ignoring case and punctuation, so "McDonald's, Indiranagar"
and "Mcdonalds - HAL Road" are one brand.

- `restaurant search` already carries an ETA per row, so it marks
  `nearest_branch` for free.
- `restaurant dish` has no ETA, so it looks one up **only for chains that
  appear more than once** — a list of distinct restaurants costs nothing extra.
  It then keeps the nearest and drops the rest, reporting both sides under
  `brand_choices`. `--all-branches` keeps them, still annotated with
  `skip_reason`.

When fewer than two outlets of a chain have a usable ETA, nothing is ranked or
dropped: picking on no evidence would be worse than showing both.

## Why the offer engine probes

Swiggy's coupon listing is misleading in two specific ways:

1. *"Add ₹149 more"* is a **shortfall**, not a saving. Read naively, it ranks
   the worst coupon first.
2. Coupons **do not stack on already-discounted items**. A combo from a "50%
   off" section makes every coupon ineligible — yet the listing still shows a
   plain value shortfall, as though spending more would fix it.

So `best-offer` applies candidates for real and keeps whichever actually lowers
the bill, then re-applies the winner last. `--probe N` controls how many
text-ineligible coupons to try anyway (default 4); `--probe 0` trusts the text.

`restaurant topup` solves "what is the least I can add to cross the threshold"
as a bounded knapsack over the live menu, biased toward filler a person would
want (a drink, a side) over a pile of sachets. It reports `worth_it: false` when
the top-up costs more than it returns.

### maximize vs topup

`topup` answers "what is the least I can add to cross this threshold".
`maximize` answers "what is the cheapest this cart can be" — it applies the best
coupon first, then only tops up if that beats the discount already in hand.

The comparison always nets off the current discount. A ₹200 coupon replacing a
₹125 one is a ₹75 gain, not ₹200, and if unlocking it costs ₹89 of food the bill
goes **up** by ₹14. `--free-only` (default) refuses that; `--any-gain` allows it
when the extra food is worth more than the extra cash.

## The payment widget, and why the CLI polls

Providers tell agents not to call `check_payment_status` in a loop. That
guidance assumes the **payment widget** is doing it for them: in a browser the
widget watches the payment and calls `confirm_order` when it succeeds.

A CLI has no widget. If it does not poll, nothing does, and the order sits
PENDING forever even after the user has paid. So the CLI takes that job on:

- follows the `pollIntervalSec` the response asks for, floored at 15s, so it is
  never the tight loop the warning is about
- never sleeps past its own deadline
- calls `confirm_order` on success when the provider has not already done so
- marks the local order CONFIRMED or PAYMENT_FAILED

`--wait` on `place`/`checkout` runs the whole lifecycle in one command; `pay
wait` attaches to an order that is already pending.

## Defaults worth knowing

| Default | Why |
|---|---|
| `place --auto-coupon` on | an order is never placed at full price by omission |
| Generic UPI QR when advertised | `PayWithQR` is used only when live payment options expose it; otherwise an enabled app is required |
| `dish --limit 10` | a long list is unusable read aloud |
| `pay wait --interval 0` | follows the provider's own `pollIntervalSec`, floored at 15s |
| `maximize --free-only` on | only tops up when it costs nothing overall |
| `--payment` **required** | the provider picks silently otherwise, possibly Cash on delivery |
| Swiggy `--max-total` **required** | restaurant and Instamart orders bind `-y` to the approved amount and suppress higher payment |
| `FOOD_CLI_NO_OPEN=1` | stop auto-opening images on headless/agent machines |

## Delivery fees

Measured on Instamart:

| Goods | Paid | Fees |
|---|---|---|
| ₹104 | ₹187 | ₹83 |
| ₹216 | ₹228 | ₹12 |

Crossing roughly **₹199** collapses the fee. It varies by city, account and
membership, so it is only a default:

```bash
food config free_delivery_threshold 249
food im fees
```

`im checkout` requires the exact approved cart total via `--max-total` and
blocks (exit 3) when fees are disproportionate.

Restaurant delivery charges can also be recomputed between a cart preview and
`place_food_order`. Always pass the confirmed preview amount to
`restaurant place --max-total <amount>`. The CLI checks once before placement
and again against the placement response. If Swiggy introduces a fee during the
placement call, it does not render, open, poll, or confirm that payment; exit 5
returns `blocked_total_changed` and the agent must obtain fresh consent.

Immediately before the cart/payment preflight, placement searches for the
exact restaurant id and requires its live `availabilityStatus` to be `OPEN`.
`CLOSED`, `UNAVAILABLE`, a missing status, or a lookup failure blocks before
`place_food_order`.

## Zepto specifics

**Store context is mandatory.** Search and cart both fail with "Store not
selected" until an address is chosen, because the address determines the store.
`food zepto use-address <id>` does both; the CLI applies the saved one
automatically and errors clearly if there is none.

**Addresses come back as prose with the ids in a trailing block:**

```
1. Home: 10 Baker Street, ...

Address IDs:
1. "Home" → ID: 1111...
```

`food zepto addresses` pairs them up. Zepto asks that ids are not shown to the
user — refer to an address by label or number.

**Search the way Zepto asks.** `get_past_order_items` (`food zepto usual`)
returns the exact product names the user buys; searching with those names is
what makes "milk" resolve to their brand. Use `search-many` for a list of
products — one search per item, grouped — and resolve a dish into ingredients
before searching.

**Cart items need two ids**, both from a search result:
`--item <productVariantId>:<storeProductId>:<qty>`. Quantity 0 removes. The
cart is keyed by a `deviceId`, generated once and kept locally so the cart
persists between commands.

**Payment method picks the tool**, not a parameter: `create_order` (COD),
`create_online_payment_order`, `create_wallet_order`,
`create_upi_reserve_pay_order`. This is the only provider here where a fully
hands-free order is possible, because COD needs no payment interaction.

Zepto rate-limits: a burst of searches returns `Too Many Requests` as prose with
`isError: false`. The CLI annotates that as `upstream_error` and
`rate_limited: true` — back off rather than treating it as data.

## Preferences: learned vs stated

```bash
food prefs show          # value + source + confidence + evidence
food prefs learn         # derive from order history
food prefs set diet '"vegetarian"'
food prefs forget        # drop learned only; --all drops stated too
```

`source: learned` is a guess with evidence attached; `source: explicit` is
something the user said. **Learned never filters results** — the user may be
ordering for a guest, or the guess may be wrong, and a hands-free user cannot
see what was hidden. `suggest` reports `diet_note` instead; ask, or pass `--veg`.

**Never use `prefs set` to record your own inference** — it marks it as
user-stated and becomes indistinguishable from fact afterwards.

Diet detection uses Swiggy's own per-item Veg/Non-veg classification where
present; the name heuristic is only a fallback. Swiggy's `vegFilter` leaks
non-veg items, so the CLI re-checks.

## Logging to expense and calorie trackers

After an order is **confirmed** (never before), check whether the user has an
expense tracker, calorie/nutrition tracker or health-logging skill. If so, log
there too — otherwise it drifts every time they order through you.

**Expenses** — straight from the order:

```bash
food orders list --limit 1     # id, vendor, amount, kind
```

**Calories** — no provider returns nutrition data. So:

1. Take item names from the confirmation or `orders stats`.
2. Research typical values — brand-published figures first, otherwise a
   reputable database for a comparable dish.
3. Log per item, scaled by quantity.

**State the uncertainty.** A named chain item is close to exact; "paneer tikka
from a local restaurant" can be off by a third. Say which it is. If the user
tracks calories for a medical reason, give a range, not a single number. Do not
skip the step because it is uncertain — a labelled estimate beats a gap.

## Order history and analytics

```bash
food orders sync                 # pull history into SQLite (safe to re-run)
food orders stats                # top items, spend by vendor
food orders spend --days 30
food orders list --remote        # live from the provider instead of local
```

`sync` never overwrites an order this CLI placed itself, so re-running is
harmless. Use `stats` to answer "the usual" and infer taste without asking again.

## Images

```bash
food restaurant dish "biryani" --images            # adds image_url
food restaurant dish "biryani" --images --download # saves locally, returns paths
```

Image URLs come from the restaurant menu (dish search has none), so `--images`
costs one extra menu fetch per restaurant, cached for an hour.

**Swiggy has no dish descriptions** — there is no description field. Do not
present an invented one as if it came from the restaurant. Say what you know
generally and make clear it is your own knowledge, or show the picture.

`local_image` is for **your** use: hand it to a renderer or attach it. Never
read the path aloud. Same for the UPI QR.

## Why all payment artefacts matter

`place`, `checkout` and `pay qr` return `qr_png`, `app_intent`, `upi_id` and
`payment_link` together. They are not alternatives:

| Artefact | Works when | Fails when |
|---|---|---|
| `payment_link` (https) | anywhere — every client linkifies https; tapping it on a phone opens the UPI app | payment already completed or lapsed |
| `qr_png` | paying from a second device with a camera | no camera, screen reader, one device |
| `upi_id` + `amount` | manual fallback in the selected app | needs manual entry and dictation |
| `app_intent` | the exact UPI app the user selected | most chat clients will not linkify a custom scheme, so it arrives as dead text |

The CLI returns all four and expresses no preference — the caller knows its own
channel. `payment_link` is the same app-specific intent behind an https
redirect, so it survives clients that do not linkify custom schemes and carries payee, amount and
transaction ref so the UPI app opens pre-filled. Resolving it does not consume
it, but it stops working once the payment completes or lapses.

## Choosing a UPI route

Food payment options expose two provider-owned surfaces. A desktop
`PayWithQR` method authorises generic UPI with `generateUPIQR=true` and no
`intentApp`; this is the default when it is live. Mobile app methods authorise
only their exact app-specific `intentApp` value. A caller cannot force generic
UPI with a CLI flag—the live MCP capability controls whether it is available.

The route is selected from live `get_payment_options` data:

- explicit enabled choice → validate it, save it as `preferred_upi_app`, and
  use the app-specific route even if generic QR is available
- no explicit choice + `PayWithQR` advertised → use generic UPI QR
- no generic QR + saved app enabled → reuse that exact app
- saved app unavailable → stop and ask again; never fall back to another app
- no generic QR or UPI app intent → **refuse to place the order** (exit 3,
  `blocked_no_payable_upi`) rather than create a charge nobody can settle

`--intent-app <id-or-exact-name>` is only for the app the user chose. When
generic QR is unavailable, a first UPI attempt without a saved app exits 3 as
`blocked_upi_app_choice` and returns the live `available` list.

Whatever path is taken, `place`, `checkout` and `pay qr` always return a flat
`payment` block with `qr_png`, `payment_link`, `app_intent` and `upi_id` keys —
present even when empty, so a caller never has to work out which shape the
response took. `payment_link` is recovered from `Payment link:` prose, a
`bridgeUrl`/`paymentLink` field, or any `mcp.swiggy.com` URL in the response.

## Payment findings

- **On Swiggy, cards, netbanking and wallets are not available.** Only UPI and
  Cash.
- `SwiggyPay` appears in the Instamart `checkout` schema but is **never**
  returned by `get_payment_options`. Tested with a funded wallet: 8 methods came
  back (7 UPI + Cash), all enabled, no wallet entry. **A top-up does not enable
  it** — do not send the user to add money.
- Swiggy food has no cash option, so a food order always ends with a UPI step.
- Zepto offers COD, an online payment link, Zepto Cash and UPI Reserve Pay.
- Availability is per account and per cart, so read `payment-options` live.

## Storage and migrations

State lives in `~/.food-cli/food.db` (mode `0600`). `FOOD_CLI_DB` overrides the
file; `FOOD_CLI_HOME` overrides the directory. A database left by the earlier
`swiggy-cli` is reused where it exists.

The schema is Alembic-managed under `food_cli/migrations/`. `store.connect()`
upgrades to head on first use per process, so there is nothing to run by hand.
A database created before migrations existed is **adopted** — stamped at the
baseline revision rather than having the baseline re-applied over tables that
already exist.

```bash
uv run alembic current
uv run alembic revision -m "add something"    # revisions are hand-written
```

## Updating

`food update` fetches, fast-forwards and runs `uv sync`, stashing and restoring
local edits around it. It refuses rather than guesses:

| Exit | Situation | What it did |
|---|---|---|
| 0 | current, or behind | nothing, or fast-forwarded and restored the stash |
| 2 | not a git checkout | nothing; reinstall instead |
| 3 | branch diverged, or the merge failed | nothing; a failed merge puts the stash back first |
| 4 | restoring the stash conflicted | updated, conflicts left in the tree, stash retained |

It only ever fast-forwards. Local commits are the user's, and merging or
rebasing them is not this command's call. On exit 4 nothing is lost — the work
is both in the tree and still in the stash; resolve the named files, then
`git stash drop`.

`skill_changed` in the output is true when SKILL.md or REFERENCE.md moved, which
is the cue for an agent to re-read its own contract.

## Full command list

```bash
food --help
food <group> --help    # auth, address, restaurant, im, zepto, orders, pay, prefs, mcp
```

| Group | Commands |
|---|---|
| `auth` | serve, url, paste, login, wait, status, refresh, logout, whoami |
| `address` | list, search, set-default, default |
| `restaurant` | search, dish, menu, cart, add, edit, remove, set-qty, clear, eta, coupons, best-offer, topup, maximize, apply-coupon, payment-options, place, suggest |
| `im` | search, usual, cart, fees, payment-options, add, clear, checkout |
| `zepto` | whoami, addresses, use-address, serviceable, usual, search, search-many, product, cart, add, remove, clear, payment-options, place, pay-status, orders, order, call |
| `orders` | list, sync, stats, spend, track |
| `pay` | qr, status, wait, confirm |
| `prefs` | learn, show, set, forget |
| `mcp` | providers, list, roles, call |
| top level | config, update |
