# Security

## What this tool stores, and where

| Data | Location | Permissions |
|---|---|---|
| OAuth access token | `~/.food-cli/food.db` | dir `0700`, file `0600` |
| Saved delivery addresses (names, phone numbers, coordinates) | same DB | `0600` |
| Order history and learned preferences | same DB | `0600` |
| Generated UPI QR codes (encode a live payment intent) | `~/.food-cli/qr/` | dir `0700`, files `0600` |
| Downloaded dish images | `~/.food-cli/media/` | dir `0700`, files `0600` |

Nothing is written to `/tmp`, which is world-readable and shared between local
users. Override the database location with `SWIGGY_CLI_DB` and the image cache
with `SWIGGY_CLI_MEDIA`.

**Nothing leaves your machine** except calls to Swiggy's own MCP endpoints.
There is no telemetry, no analytics, and no third-party service.

## What this tool never handles

- **Passwords and OTPs.** Sign-in happens in your own browser via OAuth 2.0 with
  PKCE. The CLI only ever sees the authorization code you paste back.
- **UPI PINs, card numbers, CVVs.** Payment is completed by you in your own UPI
  app. The CLI renders a QR or an app intent and stops there.

No code path prints or logs an access or refresh token. `food auth status`
reports only `has_refresh_token: true|false` and an expiry, never a value.

## Threat model: responses are untrusted input

Tool responses come from a remote server. Anything derived from them —
image URLs, the payment "bridge" page — is treated as untrusted:

- **SSRF.** Fetches are restricted to `https` with a public destination.
  Loopback, private, link-local (including `169.254.169.254` cloud metadata),
  multicast and reserved addresses are refused. Redirects are followed manually
  so **every hop is re-validated** — a permitted host cannot bounce the client
  onto an internal address. Response bodies are capped at 10 MB.
- **Arbitrary URI handling.** `open` on macOS dispatches on scheme, so a
  `file://` path or a custom app scheme in a response would otherwise be
  launched verbatim. Only `https` URLs and files inside this tool's own
  directories are ever passed to the OS opener.
- **Symlink attacks.** Downloads are written with `O_NOFOLLOW` and replace any
  pre-existing symlink, so a planted link cannot redirect a write.

## SQL

All user- and response-derived values are passed as bound parameters. The only
string-interpolated SQL is the schema migration, which interpolates table and
column names — SQLite cannot bind identifiers — drawn exclusively from a
hardcoded constant in `store.py`. Do not make that dynamic.

## Ordering safeguards

Spending money requires an explicit flag; these are deliberate speed bumps, not
suggestions.

| Guard | Behaviour | Override |
|---|---|---|
| No confirmation | `place` / `checkout` refuse to run | `--yes` / `-y` |
| Disproportionate delivery fees | blocked, exit 3 | `--ignore-fees` |
| Card-only offers you cannot use on UPI | blocked, exit 3 | `--ignore-card-offers` |
| A small top-up would unlock a coupon | blocked, exit 4 | `--ignore-near-misses` |

## Reporting

Found something? Open an issue — or, for anything exploitable, contact the
maintainer privately rather than filing publicly.

## Dependencies

Five runtime dependencies, all widely used: `httpx`, `mcp`, `rich`, `segno`,
`typer`. Keep them current with `uv sync --upgrade`.
