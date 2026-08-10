"""Unit tests for the notification channels/routing config (pure — no DB/crypto)."""
import re

from beacon_core.notifications import config as N
from beacon_core.notifications.senders import build_telegram_body, _TELEGRAM_LIMIT


def _tags_balanced(body: str) -> bool:
    opened, closed = re.findall(r"<(\w+)>", body), re.findall(r"</(\w+)>", body)
    return opened == closed        # same tags, same order (b, pre)


def test_telegram_body_normal_is_unchanged():
    # short messages: <b>subject</b> + <pre>detail</pre>, byte-for-byte as before.
    assert build_telegram_body("Daily summary", "TP1 — tp_hit") == \
        "<b>Daily summary</b>\n<pre>TP1 — tp_hit</pre>"
    assert build_telegram_body("Daily summary", "") == "<b>Daily summary</b>"
    assert build_telegram_body("Daily summary", "   ") == "<b>Daily summary</b>"


def test_telegram_body_oversized_stays_valid():
    # 5 KB detail: must fit the limit AND stay balanced (the #76 regression).
    body = build_telegram_body("broker_error", "A" * 5000)
    assert len(body) <= _TELEGRAM_LIMIT
    assert _tags_balanced(body)                       # </pre> not lost
    assert body.endswith("…(truncated)</pre>")


def test_telegram_body_oversized_with_entities_not_split():
    # payload full of < & > — escaping AFTER trim means no bisected entity.
    body = build_telegram_body("broker_error", "<b>&x" * 1500)
    assert len(body) <= _TELEGRAM_LIMIT
    assert _tags_balanced(body)
    assert "&am" not in body.replace("&amp;", "")     # no dangling half-entity
    assert "<pre>" in body and body.endswith("</pre>")


def test_catalog_shape():
    cat = N.catalog()
    assert [c["id"] for c in cat["channels"]] == \
        ["email", "telegram", "whatsapp", "sms", "webhook", "push"]
    assert len(N.EVENT_IDS) == 13          # +arm_dark (#200)
    # every channel has at least one secret field except... all have config
    assert all("fields" in c and c["fields"] for c in cat["channels"])


def test_defaults():
    d = N.sanitize_config(None)
    assert d["channels"]["email"]["enabled"] is False
    assert d["channels"]["email"]["smtp_port"] == 587
    assert d["channels"]["email"]["use_tls"] is True
    assert d["routing"]["new_signal"] == []


def test_sanitize_coerces_and_filters():
    raw = {
        "channels": {
            "email": {"enabled": True, "smtp_port": "465", "use_tls": "yes"},
            "bogus": {"enabled": True},                       # unknown -> dropped
        },
        "routing": {
            "new_signal": ["whatsapp", "sms", "telegram", "whatsapp", "nope"],
            "tp_hit": ["telegram"],
            "unknown_event": ["telegram"],                    # unknown -> dropped
        },
    }
    s = N.sanitize_config(raw)
    assert "bogus" not in s["channels"]
    assert s["channels"]["email"]["smtp_port"] == 465          # str -> int
    assert s["channels"]["email"]["use_tls"] is True
    assert s["routing"]["new_signal"] == ["whatsapp", "sms", "telegram"]  # dedup + filter
    assert s["routing"]["tp_hit"] == ["telegram"]
    assert "unknown_event" not in s["routing"]


def test_secret_passthrough_and_masking():
    raw = {"channels": {"telegram": {"enabled": True, "bot_token_enc": "gAAA-enc",
                                     "chat_id": "-100"}}}
    s = N.sanitize_config(raw)
    assert s["channels"]["telegram"]["bot_token_enc"] == "gAAA-enc"  # opaque passthrough

    pub = N.public_config(raw)
    tg = pub["channels"]["telegram"]
    assert "bot_token_enc" not in tg
    assert tg["has_bot_token"] is True
    assert pub["channels"]["email"]["has_smtp_password"] is False


def test_select_field_clamped_to_options():
    s = N.sanitize_config({"channels": {"webhook": {"method": "DELETE"}}})
    assert s["channels"]["webhook"]["method"] == "POST"        # invalid -> default


def test_sanitize_config_carries_templates():
    s = N.sanitize_config({"templates": {
        "trade_closed": {"subject": "Closed {symbol}", "body": "P&L {pl}"},
        "bogus": {"subject": "x"},                              # unknown event -> dropped
    }})
    assert s["templates"]["trade_closed"] == {"subject": "Closed {symbol}", "body": "P&L {pl}"}
    assert "bogus" not in s["templates"]
    # default config has an empty templates map (no regression on old stored blobs)
    assert N.sanitize_config(None)["templates"] == {}


def test_catalog_exposes_field_descriptor_and_sample():
    cat = N.catalog()
    tokens = {f["token"] for f in cat["fields"]}
    assert {"symbol", "channel", "pl", "ai.verdict"} <= tokens
    # the sample resolves every advertised token (picker/renderer parity)
    from beacon_core.notifications import templates as T
    for f in cat["fields"]:
        assert T.render("{" + f["token"] + "}", cat["sample"]) == f["example"]


def test_catalog_exposes_per_event_fields_and_emitted():
    cat = N.catalog()
    # per-event lists are honest: trade_closed carries channel + account (enriched)
    tc = {f["token"] for f in cat["fields_by_event"]["trade_closed"]}
    assert {"pl", "channel", "account"} <= tc
    # order_filled does NOT carry channel/account -> not advertised
    of = {f["token"] for f in cat["fields_by_event"]["order_filled"]}
    assert "channel" not in of and "account" not in of
    # non-emitted events are flagged and empty
    assert cat["emitted"]["daily_summary"] is False
    assert cat["fields_by_event"]["daily_summary"] == []
    assert cat["emitted"]["trade_closed"] is True
    assert set(cat["samples"]["new_signal"]) == {"symbol", "direction", "entry", "sl", "tp", "channel"}
