"""Dispatch routing/gating + message formatting. Imports the crypto/settings
stack, so this runs in CI (deps installed), not on a bare dev box."""
import asyncio

from beacon_core.notifications import dispatch as D
from beacon_core.notifications import senders as S


def test_format_message():
    subj, text = D.format_message("tp_hit", {"symbol": "XAUUSD", "direction": "BUY",
                                             "pl": "12.5", "detail": "TP1 — tp_hit"})
    assert subj == "[Beacon] Take-profit hit — XAUUSD"
    assert "Direction: BUY" in text and "P&L: 12.5" in text and "TP1 — tp_hit" in text


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
