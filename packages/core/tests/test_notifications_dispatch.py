"""Dispatch routing/gating + message formatting. Imports the crypto/settings
stack, so this runs in CI (deps installed), not on a bare dev box."""
import asyncio

from beacon_core.notifications import dispatch as D
from beacon_core.notifications import senders as S


def test_format_message_headline_first():
    # headline (subject): emoji + direction + symbol + label + Net P&L up front
    subj, text = D.format_message("tp_hit", {"symbol": "XAUUSD", "direction": "BUY",
                                             "pl": "12.5", "detail": "TP1 — tp_hit"})
    assert subj.startswith("🎯")                       # TP triage emoji
    for piece in ("BUY", "XAUUSD", "Take-profit hit", "P&L +12.50"):
        assert piece in subj, (piece, subj)
    # symbol/direction/P&L moved OUT of the detail rows into the headline
    assert "TP1 — tp_hit" in text
    assert "Direction:" not in text and "P&L:" not in text


def test_format_message_negative_pl_and_aligned_rows():
    subj, text = D.format_message("sl_hit", {"symbol": "XAUUSD", "pl": -40,
                                             "price": "2400", "account": "Gold"})
    assert subj.startswith("🔴") and "P&L -40.00" in subj
    # rows are column-aligned (the colon+pad makes "Price:" and "Account:" line up)
    assert "\n" in text and "Price:" in text and "Account:" in text


class _FakeResp:
    status_code = 200
    text = "ok"


class _FakeClient:
    last = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.last = json
        return _FakeResp()


def test_send_telegram_escapes_and_uses_html():
    import httpx
    orig = httpx.AsyncClient
    httpx.AsyncClient = _FakeClient
    try:
        # injection-y content that legacy Markdown would 400 on
        subj = "🎯 BUY XAUUSD — Take-profit hit"
        text = "Source: @Gold_Signals_VIP*\ndetail <x> & y"
        asyncio.run(S.send_telegram({"bot_token": "t", "chat_id": "1"}, subj, text))
        body = _FakeClient.last
    finally:
        httpx.AsyncClient = orig
    assert body["parse_mode"] == "HTML"
    assert "<b>" in body["text"]                        # headline bolded
    assert "&lt;x&gt; &amp; y" in body["text"]          # <, >, & escaped
    assert "@Gold_Signals_VIP*" in body["text"]         # _ and * pass through literally (no 400)


def test_notify_routes_enabled_and_gates_rest():
    sent = []

    async def fake(cfg, subject, text):
        sent.append((cfg.get("chat_id"), subject))

    async def fake_get_setting(session, key, default=None):
        return {
            "channels": {
                "telegram": {"enabled": True, "bot_token_enc": "tok", "chat_id": "1"},
                "sms": {"enabled": False, "auth_token_enc": "x"},
            },
            "routing": {"tp_hit": ["telegram", "sms", "push"]},  # push has no sender
        }

    orig_get, orig_senders = D.get_setting, dict(S.SENDERS)
    D.get_setting = fake_get_setting
    S.SENDERS["telegram"] = fake
    S.SENDERS["sms"] = fake
    try:
        r = asyncio.run(D.notify(None, "tp_hit", {"symbol": "XAUUSD"}))["results"]
        assert r["telegram"] == "ok"        # enabled + routed -> sent
        assert r["sms"] == "disabled"       # routed but channel off
        assert r["push"] == "no_sender"     # routed but no sender built
        assert len(sent) == 1
        # an event with no route sends nothing
        assert asyncio.run(D.notify(None, "new_signal", {}))["results"] == {}
    finally:
        D.get_setting = orig_get
        S.SENDERS.clear()
        S.SENDERS.update(orig_senders)


def test_send_test_unbuilt_channel():
    res = asyncio.run(D.send_test(None, "whatsapp"))   # no sender -> friendly error
    assert res["ok"] is False and "not built" in res["error"].lower()


def test_resolve_channel_passes_plaintext_and_defaults():
    stored = {"channels": {"email": {"enabled": True, "smtp_host": "smtp.x", "smtp_port": 587,
                                     "use_tls": True, "smtp_password_enc": "plaintext-passthrough"}}}
    cfg = D.resolve_channel("email", stored)
    # decrypt() passes through non-`enc:v1:` values unchanged
    assert cfg["smtp_host"] == "smtp.x" and cfg["smtp_port"] == 587
    assert cfg["smtp_password"] == "plaintext-passthrough" and cfg["enabled"] is True
