"""Manual (paste-the-code) OAuth flow, shared by every provider.

The MCP SDK's built-in flow needs a live localhost listener. That is fine on a
desktop, but a voice/skill context often can't rely on it - the user may be on
another device entirely. This module implements the same PKCE authorization-code
flow in two discrete steps:

    food auth url --server zepto      -> prints the consent URL
    food auth paste "<redirected>"    -> exchanges the code for tokens

The user signs in in their own browser and pastes back the URL they land on
(the localhost redirect will show a connection error - that is expected and
harmless; the authorization code is in the address bar).

Endpoints, scope, redirect path and client id all come from the provider
registry, because providers disagree on every one of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..core import store
from ..providers import provider_for, sibling_servers

DEFAULT_PORT = 21621
_PENDING_KEY = "oauth_pending"
_DCR_KEY = "dcr_client:{provider}:{redirect_uri}"


_COMPLETED_KEY = "oauth_completed_at"


def _cfg(server: str):
    return provider_for(server).oauth


def mark_completed() -> None:
    """Record that a sign-in finished, so any waiting listener can stop.

    The two sign-in routes (loopback listener and paste) can be running in
    different terminals, so the handoff goes through the store rather than
    process memory.
    """
    store.set_pref(_COMPLETED_KEY, time.time())


def completed_since(when: float) -> bool:
    ts = store.get_pref(_COMPLETED_KEY)
    return bool(ts and ts > when)


def redirect_uri_for(server: str = "food", port: int = DEFAULT_PORT) -> str:
    """Loopback redirect for a server, using that provider's whitelisted path.

    The host is spelled `localhost`, never `127.0.0.1`. Zepto's edge (AWS ELB)
    rejects any request whose body or query contains a literal loopback address
    with a bare 403 - registration and authorization both fail - while the exact
    same request using `localhost` succeeds. Both providers whitelist
    `localhost`, so it is the spelling that works everywhere. The listener still
    *binds* 127.0.0.1 only; this is the name, not the interface.

    In the paste flow nothing is listening, so the browser fails to connect -
    the code is still visible in the address bar.
    """
    return f"http://localhost:{port}{_cfg(server).callback_path}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def client_id_for(server: str, redirect_uri: str) -> str:
    """The client id to authorize as, registering one if the provider needs it.

    Providers that publish a fixed public client id are used as-is. Providers
    that expose a registration endpoint (RFC 7591) get one client registered per
    redirect URI - the URI is part of the registration, so a different port is a
    different client - and the result is cached so this happens once.
    """
    cfg = _cfg(server)
    if cfg.client_id:
        return cfg.client_id

    provider = provider_for(server).name
    key = _DCR_KEY.format(provider=provider, redirect_uri=redirect_uri)
    cached = store.get_pref(key)
    if cached:
        return cached

    resp = httpx.post(
        cfg.registration_url,
        json={
            "client_name": "food-cli",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": cfg.scope,
        },
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Client registration with {provider} failed "
            f"[{resp.status_code}]: {resp.text[:300]}"
        )
    cid = resp.json().get("client_id")
    if not cid:
        raise RuntimeError(f"{provider} registration returned no client_id")
    store.set_pref(key, cid)
    return cid


def build_authorize_url(server: str, redirect_uri: str | None = None) -> str:
    """Generate PKCE material, stash it, and return the consent URL.

    The redirect URI and client id are stored alongside the verifier because the
    token exchange must present exactly the same values.
    """
    cfg = _cfg(server)
    redirect_uri = redirect_uri or redirect_uri_for(server)
    client_id = client_id_for(server, redirect_uri)

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    store.set_pref(_PENDING_KEY, {
        "server": server,
        "verifier": verifier,
        "state": state,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "created_at": time.time(),
    })

    params = {
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": cfg.scope,
    }
    if cfg.resource:
        # RFC 8707. Must be sent at both /authorize and /token, or the token is
        # issued for the wrong audience and every tool call 401s.
        params["resource"] = cfg.resource
    return cfg.authorize_url + "?" + urlencode(params)


def _extract_code(pasted: str) -> tuple[str, str | None]:
    """Accept either a bare code or the full redirected URL."""
    pasted = pasted.strip()
    if pasted.startswith("http://") or pasted.startswith("https://"):
        qs = parse_qs(urlparse(pasted).query)
        if "error" in qs:
            raise ValueError(
                f"Authorization failed: {qs['error'][0]} "
                f"{qs.get('error_description', [''])[0]}"
            )
        if "code" not in qs:
            raise ValueError("No ?code= found in that URL.")
        return qs["code"][0], qs.get("state", [None])[0]
    return pasted, None


def exchange(pasted: str, apply_to_all: bool = True) -> dict:
    """Swap the authorization code for tokens and persist them."""
    pending = store.get_pref(_PENDING_KEY)
    if not pending:
        raise RuntimeError("No pending authorization. Run `food auth url` first.")

    server = pending["server"]
    cfg = _cfg(server)

    code, state = _extract_code(pasted)
    pasted_is_url = pasted.strip().startswith(("http://", "https://"))
    if pasted_is_url and not state:
        # A redirect URL always carries back the state we sent; one without it
        # did not come from our authorization request.
        raise ValueError("Redirect URL has no ?state= - restart with `food auth url`.")
    if state and state != pending["state"]:
        raise ValueError("State mismatch - possible CSRF. Restart with `food auth url`.")

    client_id = pending.get("client_id") or cfg.client_id
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": pending["verifier"],
        # Must be byte-identical to the one sent to /authorize.
        "redirect_uri": pending.get("redirect_uri", redirect_uri_for(server)),
    }
    if cfg.resource:
        data["resource"] = cfg.resource
    resp = httpx.post(
        cfg.token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed [{resp.status_code}]: {resp.text[:300]}")

    tok = resp.json()
    _persist(tok, server, apply_to_all, client_id=client_id,
             redirect_uri=pending.get("redirect_uri", redirect_uri_for(server)))
    store.set_pref(_PENDING_KEY, None)
    # Release any loopback listener still waiting for this same authorization.
    mark_completed()

    return {
        "status": "authorized",
        "provider": provider_for(server).name,
        "servers": list(_targets(server, apply_to_all)),
        "scope": tok.get("scope"),
        "expires_in": tok.get("expires_in"),
        "has_refresh_token": bool(tok.get("refresh_token")),
    }


def refresh(server: str = "food", apply_to_all: bool = True) -> dict:
    """Exchange the stored refresh token for a fresh access token.

    Raises if no refresh token is held (the user signed in before we started
    requesting offline_access, or the server declined to issue one).
    """
    cfg = _cfg(server)
    with store.connect() as c:
        row = c.execute(
            "SELECT tokens, client_info FROM oauth WHERE server=?", (server,)
        ).fetchone()
    if not row or not row["tokens"]:
        raise RuntimeError(f"No stored token for {server}. Run `food auth url --server {server}`.")

    tok = json.loads(row["tokens"])
    rt = tok.get("refresh_token")
    if not rt:
        raise RuntimeError(
            "No refresh token stored - this session predates offline_access, or "
            f"{provider_for(server).label} declined to issue one. "
            "Re-run `food auth url` once to get one."
        )

    client_id = cfg.client_id
    if not client_id and row["client_info"]:
        client_id = json.loads(row["client_info"]).get("client_id")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": client_id,
    }
    if cfg.resource:
        data["resource"] = cfg.resource
    resp = httpx.post(
        cfg.token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Refresh failed [{resp.status_code}]: {resp.text[:300]}")

    new = resp.json()
    # Some servers omit the refresh token on rotation - keep the old one then.
    new.setdefault("refresh_token", rt)
    _persist(new, server, apply_to_all, client_id=client_id,
             redirect_uri=redirect_uri_for(server))

    return {
        "status": "refreshed",
        "servers": list(_targets(server, apply_to_all)),
        "expires_in": new.get("expires_in"),
        "rotated_refresh_token": new.get("refresh_token") != rt,
    }


def token_info(server: str = "food") -> dict:
    """What we know about the stored token, including time left."""
    with store.connect() as c:
        row = c.execute(
            "SELECT tokens, updated_at FROM oauth WHERE server=?", (server,)
        ).fetchone()
    if not row or not row["tokens"]:
        return {"authorized": False, "provider": provider_for(server).name}
    tok = json.loads(row["tokens"])
    issued = row["updated_at"] or 0
    expires_in = tok.get("expires_in")
    expires_at = issued + expires_in if expires_in else None
    return {
        "authorized": True,
        "provider": provider_for(server).name,
        "has_refresh_token": bool(tok.get("refresh_token")),
        "scope": tok.get("scope"),
        "issued_at": issued,
        "expires_at": expires_at,
        "seconds_remaining": round(expires_at - time.time()) if expires_at else None,
        "expired": bool(expires_at and expires_at <= time.time()),
    }


def _targets(server: str, apply_to_all: bool):
    """Which server rows this token may be written to.

    Only ever siblings of the same provider. Swiggy mints one token that works
    for both food and Instamart, so one sign-in should authorize both; writing a
    Swiggy token into Zepto's row would be silent corruption.
    """
    return sibling_servers(server) if apply_to_all else [server]


def _persist(tok: dict, server: str, apply_to_all: bool,
             client_id: str | None = None, redirect_uri: str | None = None) -> None:
    """Store tokens in the shape mcp's OAuthToken model expects."""
    cfg = _cfg(server)
    payload = {
        "access_token": tok["access_token"],
        "token_type": tok.get("token_type", "Bearer"),
    }
    for k in ("expires_in", "refresh_token", "scope"):
        if tok.get(k) is not None:
            payload[k] = tok[k]

    client_info = {
        "client_id": client_id or cfg.client_id,
        "redirect_uris": [redirect_uri or redirect_uri_for(server)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": cfg.scope,
        "token_endpoint_auth_method": "none",
    }

    with store.connect() as c:
        for name in _targets(server, apply_to_all):
            c.execute(
                "INSERT INTO oauth(server,tokens,client_info,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(server) DO UPDATE SET tokens=excluded.tokens, "
                "client_info=excluded.client_info, updated_at=excluded.updated_at",
                (name, json.dumps(payload), json.dumps(client_info), time.time()),
            )
