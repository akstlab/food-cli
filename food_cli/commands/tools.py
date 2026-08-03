"""Generic MCP access: list a server's tools, call one directly, inspect roles.

This is the escape hatch. Every curated command in this CLI is a convenience
over these three, so anything the wrapper does not cover is still reachable.
"""

from __future__ import annotations

import asyncio
import json

import typer

from ..mcp import client
from ..providers import PROVIDERS, SERVERS_BY_KEY, roles as roles_mod
from .common import err, out

tools_app = typer.Typer(no_args_is_help=True, help="Raw MCP access and provider inspection.")


@tools_app.command("providers")
def list_providers():
    """List configured providers and their servers."""
    out({
        name: {
            "label": p.label,
            "servers": {s.key: {"url": s.url, "label": s.label} for s in p.servers},
            "auth": {
                "authorize_url": p.oauth.authorize_url,
                "client_id": p.oauth.client_id,
                "dynamic_registration": bool(p.oauth.registration_url),
                "scope": p.oauth.scope,
            },
        }
        for name, p in PROVIDERS.items()
    })


@tools_app.command("list")
def list_server_tools(
    server: str = typer.Argument(..., help=f"One of: {', '.join(sorted(SERVERS_BY_KEY))}"),
    schema: bool = typer.Option(False, "--schema", help="Include full input schemas."),
):
    """List the tools an MCP server exposes."""
    res = asyncio.run(client.list_tools(server))
    if not schema:
        res = [{"name": t["name"], "description": t["description"]} for t in res]
    out(res)


@tools_app.command("roles")
def show_roles(
    server: str = typer.Argument(...),
    refresh: bool = typer.Option(False, "--refresh", help="Re-discover instead of using cache."),
):
    """Show which real tool backs each capability on a server.

    Servers whose tool names are published resolve statically. Anything else is
    discovered by listing the server's tools and matching them, so this is how
    you check what the CLI decided before trusting it with an order.
    """
    known = server in roles_mod.KNOWN
    mapping = roles_mod.discover(server, refresh=refresh)
    unresolved = [r for r, v in mapping.items() if not v]
    if unresolved:
        err(f"note: no tool matched for {', '.join(unresolved)} - use `food call` for those.")
    out({
        "server": server,
        "source": "published" if known else ("discovered" if refresh else "cache_or_discovered"),
        "roles": mapping,
        "unresolved": unresolved,
    })


@tools_app.command("call")
def call_tool(
    server: str = typer.Argument(...),
    tool: str = typer.Argument(...),
    args_json: str = typer.Option("{}", "--args", help="Tool arguments as a JSON object."),
):
    """Call any MCP tool directly."""
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        err(f"--args must be valid JSON: {e}")
        raise typer.Exit(2) from e
    out(asyncio.run(client.call_tool(server, tool, args)))
