"""Sign in to a provider (OAuth 2.0 + PKCE).

The CLI never sees a password or an OTP. It produces a consent URL; the user
signs in in their own browser and the authorization code comes back to a
one-shot loopback listener (`serve`) or is pasted in (`url` + `paste`).
"""

from __future__ import annotations

import asyncio
import time

import typer

from ..core import store
from ..mcp import oauth
from ..mcp import client
from ..providers import PROVIDERS, SERVERS, provider_for, sibling_servers
from .common import err, out

auth_app = typer.Typer(no_args_is_help=True, help="Sign in to a provider (OAuth).")


def _targets(server: str) -> list[str]:
    """Expand `all`, a provider name, or a single server key into server keys."""
    if server == "all":
        return list(SERVERS)
    if server in PROVIDERS:
        return [s.key for s in PROVIDERS[server].servers]
    return [server]


@auth_app.command("login")
def auth_login(
    server: str = typer.Option("all", help="A server key, a provider name, or 'all'."),
    print_url_only: bool = typer.Option(
        False, "--print-url-only",
        help="Emit the consent URL as JSON and exit without waiting (for agents).",
    ),
):
    """Authorize the CLI. Prints a consent URL you must open."""
    results = {}
    for name in _targets(server):
        captured: dict[str, str] = {}
        label = provider_for(name).label

        def on_url(url: str, _c=captured, _n=name, _l=label):
            _c["url"] = url
            if not print_url_only:
                err(f"\n[{_n}] Open this URL and sign in to {_l}:\n\n{url}\n")

        try:
            tools = asyncio.run(client.list_tools(name, on_consent_url=on_url))
            results[name] = {"status": "authorized", "tool_count": len(tools)}
        except Exception as e:  # noqa: BLE001
            if print_url_only and captured.get("url"):
                results[name] = {"status": "consent_required", "consent_url": captured["url"]}
            else:
                results[name] = {"status": "error", "error": str(e)[:300]}
    out(results)


@auth_app.command("url")
def auth_url(server: str = typer.Option("food", help="Server the token is minted for.")):
    """Print the consent URL (step 1 of the paste flow).

    Prefer `auth serve`, which captures the redirect itself so the
    authorization code never has to be handled by anyone. Use this only when
    the listener cannot work - signing in from a different machine, say.
    """
    url = oauth.build_authorize_url(server)
    label = provider_for(server).label
    err(
        f"\nOpen this URL and sign in to {label}. You will land on a page that "
        "fails to load - that is expected.\n\n"
        "Then hand the address in the bar to whoever is driving this CLI, or "
        "run it yourself:\n"
        '    food auth paste "<that address>"\n\n'
        "Do it promptly: the code in that address expires quickly, and using "
        "it is what spends it.\n"
    )
    out({
        "consent_url": url,
        "provider": provider_for(server).name,
        "next": 'food auth paste "<redirected url or code>"',
        "note": (
            "The redirect URL carries a single-use authorization code, bound "
            "by PKCE to a verifier held only on this machine - it cannot "
            "complete a sign-in anywhere else. An assistant given this URL "
            "should pass it to `auth paste` immediately rather than refuse: "
            "spending the code is what invalidates it. Do not echo it back."
        ),
    })


@auth_app.command("serve")
def auth_serve(
    server: str = typer.Option("food", help="Server the token is minted for."),
    port: int = typer.Option(oauth.DEFAULT_PORT, "--port", help="0 picks a free port."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the redirect."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    only_server: bool = typer.Option(False, "--only-server"),
):
    """Sign in with a local callback server - no copy-pasting.

    Starts a one-shot loopback listener, opens the consent page, waits for the
    redirect, exchanges the code and shuts the server down.
    """
    import webbrowser

    from ..mcp import callback

    label = provider_for(server).label
    path = provider_for(server).oauth.callback_path
    started = time.time()

    try:
        srv = callback.CallbackServer(port=port, path=path, provider_label=label)
    except OSError as e:
        err(f"Could not bind port {port}: {e}\nTry --port 0 to pick a free one.")
        raise typer.Exit(2) from e

    with srv:
        url = oauth.build_authorize_url(server, redirect_uri=srv.redirect_uri)
        err(
            f"\nListening on {srv.redirect_uri} (loopback only)\n\n"
            f"Open this URL and sign in to {label}:\n\n"
            f"{url}\n\n"
            f"Waiting up to {timeout}s for the redirect.\n"
            'You can also finish with `food auth paste "<url>"` - either route '
            "stops this listener.\n"
        )
        if open_browser:
            webbrowser.open(url)

        try:
            # Stop as soon as *either* route completes, so the socket is never
            # left bound after sign-in has already succeeded elsewhere.
            params = srv.wait(timeout=timeout,
                              until=lambda: oauth.completed_since(started))
        except callback.ListenerCancelled:
            err("\n✅ Sign-in completed by paste; listener stopped.\n")
            out({"status": "authorized", "via": "paste", "server": server})
            return
        except TimeoutError as e:
            err(f"\n{e}\n")
            out({"status": "timeout", "consent_url": url})
            raise typer.Exit(2) from e

    if "code" not in params:
        detail = params.get("error_description") or params.get("error") or "no code returned"
        err(f"\nAuthorization failed: {detail}\n")
        out({"status": "failed", "error": detail})
        raise typer.Exit(1)

    try:
        result = oauth.exchange(
            f"{srv.redirect_uri}?code={params['code']}&state={params.get('state', '')}",
            apply_to_all=not only_server,
        )
    except Exception as e:  # noqa: BLE001
        err(f"error: {e}")
        raise typer.Exit(1) from e

    err(f"\n✅ Signed in to {label}.\n")
    out(result)


@auth_app.command("paste")
def auth_paste(
    pasted: str = typer.Argument(..., help="The redirected URL, or just the code."),
    only_server: bool = typer.Option(
        False, "--only-server",
        help="Store the token for the requested server only "
             "(default: every server of the same provider).",
    ),
):
    """Exchange the authorization code for tokens (step 2 of the paste flow)."""
    try:
        out(oauth.exchange(pasted, apply_to_all=not only_server))
    except Exception as e:  # noqa: BLE001
        err(f"error: {e}")
        raise typer.Exit(1) from e


@auth_app.command("wait")
def auth_wait(
    server: str = typer.Option("food", help="Server to verify against."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for sign-in."),
    interval: float = typer.Option(3.0, "--interval"),
):
    """Poll until sign-in completes.

    Run this straight after `food auth url`: hand the user the consent URL, then
    let this block until they have finished, instead of guessing when to retry.
    """
    deadline = time.time() + timeout
    polls = 0
    while time.time() < deadline:
        polls += 1
        with store.connect() as c:
            row = c.execute(
                "SELECT tokens FROM oauth WHERE server=? AND tokens IS NOT NULL", (server,)
            ).fetchone()
        if row:
            # Tokens exist - prove they actually work before declaring success.
            try:
                tools = asyncio.run(client.list_tools(server, on_consent_url=lambda _u: None))
                err(f"\n✅ Signed in to {provider_for(server).label}.\n")
                out({"status": "authorized", "server": server,
                     "tool_count": len(tools), "polls": polls})
                return
            except Exception:  # noqa: BLE001
                pass
        time.sleep(interval)

    err(f"\n⏳ Not signed in after {timeout}s.\n")
    out({"status": "timeout", "server": server, "polls": polls})
    raise typer.Exit(2)


@auth_app.command("status")
def auth_status():
    """Report token state per server, including expiry and refresh capability."""
    out({s: oauth.token_info(s) for s in SERVERS})


@auth_app.command("refresh")
def auth_refresh(
    server: str = typer.Option("food", help="Server whose refresh token to use."),
    only_server: bool = typer.Option(False, "--only-server"),
):
    """Renew the access token without signing in again."""
    try:
        out(oauth.refresh(server, apply_to_all=not only_server))
    except Exception as e:  # noqa: BLE001
        err(f"error: {e}")
        raise typer.Exit(1) from e


@auth_app.command("logout")
def auth_logout(
    server: str = typer.Option("all", help="A server key, a provider name, or 'all'."),
):
    """Delete stored tokens."""
    targets = _targets(server)
    with store.connect() as c:
        if server == "all":
            c.execute("DELETE FROM oauth")
        else:
            c.executemany("DELETE FROM oauth WHERE server=?", [(t,) for t in targets])
    out({"logged_out": targets})


@auth_app.command("whoami")
def auth_whoami():
    """Which providers are signed in, and what each token covers."""
    rows = {}
    for name, provider in PROVIDERS.items():
        infos = {s.key: oauth.token_info(s.key) for s in provider.servers}
        rows[name] = {
            "label": provider.label,
            "signed_in": any(i.get("authorized") for i in infos.values()),
            "servers": infos,
            "shares_token_with": {s.key: sibling_servers(s.key) for s in provider.servers},
        }
    out(rows)
