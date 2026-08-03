# food-cli

A general-purpose command-line client for ordering food and groceries, built on
the providers' **official MCP servers**. Designed to be driven by a voice agent
for accessibility, but usable standalone.

Output on `stdout` is always JSON so it can be piped into an agent. Human-facing
prompts and consent URLs go to `stderr`.

## Providers

| Group | Server | Endpoint | What it is |
|---|---|---|---|
| `restaurant` | `food` | `https://mcp.swiggy.com/food` | Swiggy restaurant delivery |
| `im` | `instamart` | `https://mcp.swiggy.com/im` | Swiggy Instamart groceries |
| `zepto` | `zepto` | `https://mcp.zepto.co.in/mcp` | Zepto quick commerce |

Adding a provider is one module under `food_cli/providers/` plus an entry in the
registry. Servers are OAuth-protected and expose their tool schemas at runtime,
so new tools become callable through `food mcp call` without a code change.

The restaurant group is named for what it sells rather than for Swiggy: the
binary is already `food`, and Instamart is also Swiggy.

## Install with an agent (fastest)

Copy the block below and paste it into your coding agent (Claude Code, Cursor,
Codex, …). Nothing to edit — it clones from GitHub, installs, signs you in and
sets up the skill.

```text
Install and set up the `food` CLI for me. Do this end to end and only stop
when you need something only I can provide.

1. Check prerequisites. It needs Python 3.13+ and `uv`. If `uv` is missing,
   install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh
2. git clone https://github.com/akstlab/food-cli.git ~/food-cli && cd ~/food-cli
3. Run ./setup.sh  (equivalently: uv sync). This creates .venv and installs the
   `food` entry point.
4. Verify with:  uv run food --help   and   uv run pytest -q
   All tests must pass. If any fail, stop and show me the failure.
5. Ask me which providers I want: Swiggy, Zepto, or both. Then sign me in to
   each one I chose:
     uv run food auth serve --server food     # Swiggy (covers Instamart too)
     uv run food auth serve --server zepto    # Zepto
   That is the whole flow where it works: it prints a consent URL, waits, and
   captures the redirect itself. Give me the URL, tell me to sign in, and let
   the command finish - it blocks for up to 5 minutes.
   If the listener cannot reach my browser (I am on another machine, or the
   port will not bind), fall back to the paste flow and RUN IT YOURSELF:
     uv run food auth url --server <server>
   Give me the consent_url. I sign in, land on a page that FAILS TO LOAD -
   expected and correct - and send you the address from the bar. Then:
     uv run food auth paste "<the url I send you>"
   Do that straight away rather than refusing: the code is single-use and
   PKCE-bound to a verifier that never leaves this machine, so spending it
   immediately is what makes it useless to anyone else. Don't repeat the URL
   back to me.
   Do NOT ask me for my password or any OTP, and do not type them yourself.
6. Set my delivery address for each provider I signed in to:
     uv run food address list                 # Swiggy
     uv run food address set-default <id> --label <name>
     uv run food zepto addresses              # Zepto, separate address book
     uv run food zepto use-address <id>
   Show me the labelled addresses and ask which to use. Refer to Zepto
   addresses by label or number, not by id.
7. Learn my preferences from my order history:
     uv run food orders sync && uv run food prefs learn
   Show me what it inferred, and tell me these are guesses I can correct with
   `food prefs set`.
8. Install the skill so you can use it in future: copy SKILL.md into wherever
   your skills live (for Claude Code: ~/.claude/skills/food/SKILL.md).
9. Confirm it works:  uv run food restaurant search "pizza"

Important, do not skip:
- Never enter or ask for my OTP, UPI PIN, or card details. A sign-in redirect
  URL is not one of those - take it and run `food auth paste` with it.
- Never place an order without showing me the items, address and total first
  and getting an explicit yes.
- Never pick a payment method for me, and never default to cash on delivery.
- For UPI, show me the live app choices and ask which app I prefer the first
  time. Save that validated choice; never assume or silently substitute an app.
- The local database at ~/.food-cli/food.db holds OAuth tokens and my
  addresses. Never commit it, print it, or copy it anywhere.
```

## Install by hand

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/akstlab/food-cli.git ~/food-cli && cd ~/food-cli && uv sync
```

This installs the `food` entry point into the project venv. Run it with
`uv run food ...`, or activate the venv to call `food` directly:

```bash
source .venv/bin/activate
food --help
```

## Sign in

Both flows use OAuth 2.0 with PKCE; **the CLI never sees your password or OTP** —
you authenticate in your own browser.

```bash
food auth serve --server food      # Swiggy: one token covers food + Instamart
food auth serve --server zepto     # Zepto
```

Starts a one-shot loopback listener, opens the consent page, captures the
redirect and shuts down. Use `--port 0` for a free port, `--timeout` to wait
longer, `--no-open` to skip launching a browser.

### Paste flow (works anywhere, no listener)

```bash
food auth url --server zepto
```

Open the printed URL, sign in, then copy the URL you are redirected to. The page
will fail to load — that is expected, nothing is listening on that port. The
authorization code is in the address bar.

```bash
food auth paste "http://localhost:21621/callback?code=...&state=..."
```

Either route stops the other: if a listener is waiting and you finish by
pasting, the listener notices and releases the port instead of holding it until
the timeout.

### Check / reset

```bash
food auth status     # per-server expiry and refresh capability
food auth whoami     # which providers are signed in, and what each token covers
food auth logout --server zepto
```

### Token lifetime differs by provider

| Provider | Access token | Refresh token |
|---|---|---|
| Swiggy | ~5 days | **Not issued.** Sign in again when it expires. |
| Zepto | ~7 days | Issued; renewed automatically. |

Swiggy's authorization server advertises the `refresh_token` grant but does not
issue one to this public client, even when `offline_access` is requested —
verified by testing. The CLI warns on stderr within 24h of expiry, and will
start refreshing automatically if that ever changes.

### Two provider quirks worth knowing

Both were found by testing against the live servers, and both are handled for
you:

- **Redirect host must be `localhost`, not `127.0.0.1`.** Zepto's edge rejects
  any request containing a literal loopback address with a bare `403` —
  registration and authorization both fail. The listener still binds `127.0.0.1`
  only; only the spelling changes.
- **Zepto binds tokens to a resource** (RFC 8707). A token minted without
  `resource=https://mcp.zepto.co.in` authenticates fine and then fails every
  tool call with *"The token is not intended for this resource"*, which surfaces
  as a re-authorization loop rather than an error.

## Usage

```bash
food restaurant search "pizza"          # Swiggy restaurants
food restaurant dish "biryani"          # a dish across all restaurants
food im search "milk"                   # Instamart
food zepto usual                        # what you reorder most on Zepto
food zepto search "amul milk"
food orders stats                       # spend and favourites, across providers
```

Discover what a server offers, and reach anything this CLI does not wrap:

```bash
food mcp providers
food mcp list zepto --schema
food mcp roles zepto                    # which real tool backs each capability
food mcp call food <tool-name> --args '{"query":"biryani"}'
```

## Storage and migrations

State lives in a local SQLite database at `~/.food-cli/food.db` (mode `0600`):
OAuth tokens, saved addresses, learned preferences and order history. Override
with `FOOD_CLI_DB`. A database left by the earlier `swiggy-cli` is reused where
it exists, so upgrading does not strand your tokens and history.

The schema is managed by [Alembic](https://alembic.sqlalchemy.org/) under
`food_cli/migrations/`. Migrations run automatically on first use, so there is
nothing to run by hand. A database created before migrations existed is adopted
(stamped at the baseline) rather than rebuilt.

```bash
uv run alembic -c alembic.ini current    # if you want to inspect it
```

## Safety model

The CLI deliberately refuses to handle two things, and hands back to a human:

- **OTPs** — never entered or stored.
- **UPI / payment confirmation** — the final, irreversible step is always yours.

Beyond that:

- `--payment` is **required** to place any order. Providers pick one silently
  otherwise, and that may be cash on delivery, which commits you to paying a
  courier you never agreed to.
- Restaurant and Instamart ordering require both explicit `-y` and
  `--max-total <amount>`; the latter must be the exact final total the user
  approved.
- Disproportionate delivery fees block checkout unless you pass `--ignore-fees`.

For restaurant and Instamart orders the CLI checks the live cart immediately
before placing, then checks the provider-returned total before opening any UPI
artefact. A total above `--max-total` exits 5 and suppresses payment, so a fee
introduced during placement cannot silently reuse consent for a lower preview.

Zepto's MCP is not a sandbox: anything ordered through it is a real order.

## Tests

```bash
uv run pytest -q
uv run pytest --cov=food_cli --cov-report=term
```

The suite mocks the MCP layer entirely — no network, no account, no personal
data. Sample restaurants, dishes and addresses in the fixtures are invented.

See [SECURITY.md](SECURITY.md) for the threat model and storage guarantees.

## Update

```bash
food update
```

That is the whole thing: it fetches, fast-forwards, and runs `uv sync`. Local
edits are stashed and put back automatically.

```bash
food update --dry-run    # what would change, without touching anything
food update --check      # also run the test suite afterwards
food update --no-sync    # skip the dependency step
```

It is deliberately unwilling to be clever, because it pulls code and then runs
it:

| Situation | What it does | Exit |
|---|---|---|
| Already current | nothing | 0 |
| Behind the remote | fast-forwards, restores your stash | 0 |
| Your branch has local commits | refuses — will not merge or rebase for you | 3 |
| Restoring your edits conflicts | leaves the conflict in the tree and names the files; nothing is lost, the stash is still there | 4 |
| Not a git checkout | tells you to reinstall | 2 |

On a conflict, resolve the listed files and then `git stash drop`. The command
never resolves or discards your work itself.

Or paste this into your agent:

```text
Update my `food` CLI installation.

1. Run `uv run food update --check` and show me what it reports.
2. If it exits 3 (my branch has diverged) or 4 (restoring my edits
   conflicted), STOP and show me the conflicting files. Do not resolve,
   merge, rebase, reset or drop anything - that is my call.
3. If `skill_changed` is true in the output, re-read SKILL.md and tell me
   what changed about how you should behave.
4. uv run food auth status
   - For any provider where `expired` is true or `seconds_remaining` is under
     a day: if it has a refresh token, run `uv run food auth refresh --server
     <server>`. If it does not (Swiggy), run `uv run food auth serve --server
     <server>`, give me the consent URL and let it capture the redirect
     itself; if that cannot reach my browser, use the paste flow and run
     `food auth paste` yourself with the URL I send. Swiggy tokens last
     ~5 days and it issues no refresh token, so this is normal.
5. Re-sync my history and preferences:
     uv run food orders sync && uv run food prefs learn
6. Copy the repo's SKILL.md over my installed copy (for Claude Code:
   ~/.claude/skills/food/SKILL.md).
7. Summarise: what changed in this update, any new commands
   (`uv run food --help`), and anything I need to do.

Do not delete ~/.food-cli/food.db — it holds my tokens, saved addresses,
order history and learned preferences.
```

If a provider changes its tool surface, re-run `food mcp list <server> --schema`
and `food mcp roles <server> --refresh` — the CLI reads schemas at runtime, so
new tools are callable via `food mcp call` without a code change.

## Privacy

The CLI stores OAuth tokens, your saved delivery addresses (which include names
and phone numbers) and your full order history locally, mode `0600`. Nothing
leaves your machine except calls to the providers themselves.

`.gitignore` excludes that database and its WAL/SHM sidecars, generated UPI QR
codes, and downloaded dish images. Nothing personal is committed.

The CLI never handles your password, OTP, UPI PIN or card details.

## License

MIT — see [LICENSE](LICENSE).
