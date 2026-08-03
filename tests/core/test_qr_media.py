"""QR rendering and image download — filesystem only, HTTP mocked."""

from __future__ import annotations

import base64

import pytest

from food_cli.core import media
from food_cli.core import qr
from food_cli.cli import app
from tests.conftest import parse_out

UPI = "upi://pay?pa=merchant@bank&pn=Store&am=430.00&cu=INR&tr=txn-1"
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ------------------------------------------------------------------ finding

def test_find_upi_intent_unescapes_html():
    found = qr.find_qr({"html": "upi://pay?pa=a@b&amp;am=10.00"})
    assert found["kind"] == "upi_uri"
    assert "&am=" in found["value"] and "&amp;" not in found["value"]


def test_html_parser_handles_js_and_unicode_escaping():
    page = (
        r'<script>window.payment={upiIntentUrl:"upi:\/\/pay?pa=merchant@bank'
        r'\u0026am=155.00\u0026cu=INR"}</script>'
    )
    intent = qr.intent_from_html(page)
    assert intent == "upi://pay?pa=merchant@bank&am=155.00&cu=INR"


def test_structured_widget_result_is_preferred():
    found = qr.find_qr({
        "content": "Payment pending.",
        "structuredContent": {
            "data": {"upiIntentUrl": UPI, "isQrFlow": True},
        },
    })
    assert found == {"kind": "upi_uri", "value": UPI}


@pytest.mark.parametrize("source", [
    "paytmmp://upi/pay?pa=merchant@bank&am=204.00&cu=INR",
    "paytm://upi/pay?pa=merchant@bank&am=204.00&cu=INR",
    "paytmmp%3A%2F%2Fupi%2Fpay%3Fpa%3Dmerchant%40bank%26am%3D204.00%26cu%3DINR",
    (
        "paytmmp://cash_wallet?url="
        "upi%3A%2F%2Fpay%3Fpa%3Dmerchant%40bank%26am%3D204.00%26cu%3DINR"
    ),
])
def test_paytm_intents_keep_the_selected_app_scheme(source):
    intent = qr.normalise_intent(source)
    assert intent is not None
    assert intent.startswith(("paytmmp://", "paytm://"))
    assert qr._payee_of(intent) == "merchant@bank"
    assert qr._amount_of(intent) == 204.0


def test_find_returns_none():
    assert qr.find_qr({"nothing": "here"}) is None


def test_find_bridge_resolves_to_intent(monkeypatch):
    page = f'<html><a href="{UPI}&amp;tn=Pay">x</a></html>'.encode()
    monkeypatch.setattr(qr.security, "safe_get", lambda *a, **k: page)

    found = qr.find_qr({"bridgeUrl": "https://mcp.example.com/deeplink-redirect?link=z&mode=qr"})
    assert found["kind"] == "upi_uri"
    assert found["value"].startswith("upi://")


def test_find_bridge_unresolvable_falls_back(monkeypatch):
    monkeypatch.setattr(qr.security, "safe_get", lambda *a, **k: None)
    found = qr.find_qr({"bridgeUrl": "https://mcp.example.com/deeplink-redirect?link=z"})
    assert found["kind"] == "image_url"


def test_extract_order_id_variants():
    assert qr.extract_order_id('{"orderId":"12345678"}') == "12345678"
    assert qr.extract_order_id({"orderId": "78901234"}) == "78901234"
    # unquoted key, as the live PENDING_PAYMENT text uses
    assert qr.extract_order_id("- orderId: \"888000100000019\"") == "888000100000019"


# ---------------------------------------------------------------- amounts

@pytest.mark.parametrize("uri,amount,payee", [
    (UPI, 430.0, "merchant@bank"),
    ("upi://pay?pa=x@y", None, "x@y"),
    ("upi://pay?am=notanumber", None, None),
])
def test_amount_and_payee(uri, amount, payee):
    assert qr._amount_of(uri) == amount
    assert qr._payee_of(uri) == payee


# --------------------------------------------------------------- rendering

def test_present_writes_png_and_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    res = qr.present({"kind": "upi_uri", "value": UPI}, order_ref="ord-1", open_browser=False)
    assert res["png"].endswith("ord-1.png")
    assert res["svg"].endswith("ord-1.svg")
    assert (tmp_path / "ord-1.png").stat().st_size > 0
    assert res["amount"] == 430.0
    assert res["payee"] == "merchant@bank"


def test_present_image_url(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    res = qr.present({"kind": "image_url", "value": "https://x/y.png"}, open_browser=False)
    assert res["url"] == "https://x/y.png"


def test_qr_files_are_owner_only(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    res = qr.present({"kind": "upi_uri", "value": UPI}, order_ref="perm", open_browser=False)
    assert oct(os.stat(res["png"]).st_mode & 0o777) == "0o600"


def test_open_refuses_untrusted_targets(capsys):
    """`open` dispatches on scheme, so only https or our own files are allowed."""
    for bad in ("file:///etc/passwd", "myapp://run?x=1", "/etc/passwd",
                "http://example.com/x"):
        assert qr._open(bad) is False
    assert "refusing to open" in capsys.readouterr().err


def test_open_allows_our_own_file(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    f = tmp_path / "ok.png"
    f.write_bytes(PNG_1x1)
    monkeypatch.setattr(qr.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(qr.sys, "platform", "darwin")
    assert qr._open(str(f)) is True


# ----------------------------------------------------------- media download

def test_download_caches_by_url(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return PNG_1x1

    monkeypatch.setattr(media.security, "safe_get", fake_get)
    p1 = media.download("https://cdn.example.com/a.jpg", tmp_path)
    p2 = media.download("https://cdn.example.com/a.jpg", tmp_path)
    assert p1 == p2
    assert len(calls) == 1                     # second call served from disk


def test_download_handles_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(media.security, "safe_get", lambda *a, **k: None)
    assert media.download("https://cdn.example.com/missing.jpg", tmp_path) is None


def test_download_rejects_unsafe_url(tmp_path):
    """SSRF guard: internal targets must never be fetched."""
    assert media.download("http://169.254.169.254/latest/meta-data/", tmp_path) is None
    assert media.download("file:///etc/passwd", tmp_path) is None
    assert media.download("https://127.0.0.1/x", tmp_path) is None


def test_download_none_url(tmp_path):
    assert media.download("", tmp_path) is None


def test_download_many(tmp_path, monkeypatch):
    monkeypatch.setattr(media.security, "safe_get", lambda *a, **k: PNG_1x1)
    got = media.download_many(
        ["https://x/a.jpg", "https://x/b.png", "", "https://x/a.jpg"], tmp_path)
    assert len(got) == 2


def test_download_many_empty():
    assert media.download_many([]) == {}


# ------------------------------------------------------------------- pay CLI

def test_pay_qr_from_upi_uri(runner, mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "QR_DIR", tmp_path)
    r = runner.invoke(app, ["pay", "qr", UPI, "--order-id", "o-1", "--no-open"])
    assert r.exit_code == 0
    assert parse_out(r)["png"].endswith("o-1.png")


def test_pay_qr_unknown_input(runner, mcp):
    assert runner.invoke(app, ["pay", "qr", "not-a-qr"]).exit_code == 2


def test_pay_status(runner, mcp):
    assert runner.invoke(app, ["pay", "status", "ppp-0001", "--kind", "instamart"]).exit_code == 0


def test_pay_confirm(runner, mcp):
    r = runner.invoke(app, ["pay", "confirm", "8880001", "--kind", "instamart"])
    assert r.exit_code == 0


def test_pay_wait_detects_success(runner, mcp):
    r = runner.invoke(app, ["pay", "wait", "ppp-0001", "--order-id", "8880001",
                            "--kind", "instamart", "--timeout", "5"])
    assert r.exit_code == 0
    assert parse_out(r)["status"] == "paid"


def test_pay_wait_detects_failure(runner, mcp):
    mcp.set("instamart", "check_payment_status",
            ["failed", {"status": "failed", "isTerminalFailure": True}])
    r = runner.invoke(app, ["pay", "wait", "ppp-0001", "--order-id", "8880001",
                            "--kind", "instamart", "--timeout", "5"])
    assert r.exit_code == 1
    assert parse_out(r)["status"] == "failed"


def test_pay_wait_times_out(runner, mcp):
    mcp.set("instamart", "check_payment_status", ["pending", {"status": "pending"}])
    r = runner.invoke(app, ["pay", "wait", "ppp-0001", "--order-id", "8880001",
                            "--kind", "instamart", "--timeout", "1", "--interval", "0.2"])
    assert r.exit_code == 2
    assert parse_out(r)["status"] == "timeout"


def test_pay_wait_needs_a_paas_id(runner, mcp):
    assert runner.invoke(app, ["pay", "wait", "--kind", "food"]).exit_code == 2
