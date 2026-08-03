"""food — order food and groceries from the command line.

A general-purpose CLI over the providers' own official MCP servers (Swiggy for
restaurant food and Instamart, Zepto for quick commerce), built so an assistant
can drive it on behalf of someone who cannot use an app.

Design rules:
  * stdout is always machine-readable JSON (agents parse it); human chatter and
    consent URLs go to stderr.
  * OTP and UPI/payment confirmation are NEVER handled here - the CLI stops and
    hands back to the human.
"""

from __future__ import annotations

import typer

from .commands.address import addr_app
from .commands.auth import auth_app
from .commands.config import config
from .commands.food import food_app
from .commands.instamart import im_app
from .commands.orders import orders_app
from .commands.pay import pay_app
from .commands.prefs import prefs_app
from .commands.tools import tools_app
from .commands.update import update
from .commands.zepto import zepto_app

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

app.add_typer(auth_app, name="auth")
app.add_typer(addr_app, name="address")
# Named for what it sells, not for the vendor: the binary is already `food`, so
# `food food search` would be absurd, and Instamart is also Swiggy.
app.add_typer(food_app, name="restaurant")
app.add_typer(im_app, name="im")
app.add_typer(zepto_app, name="zepto")
app.add_typer(orders_app, name="orders")
app.add_typer(pay_app, name="pay")
app.add_typer(prefs_app, name="prefs")
app.add_typer(tools_app, name="mcp")

app.command("config")(config)
app.command("update")(update)


def main():
    app()


if __name__ == "__main__":
    main()
