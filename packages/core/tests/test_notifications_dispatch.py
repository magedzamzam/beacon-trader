"""Dispatch routing/gating + message formatting. Imports the crypto/settings
stack, so this runs in CI (deps installed), not on a bare dev box."""
import asyncio

from beacon_core.db.models import NotificationDelivery
from beacon_core.notifications import deliveries as DL
from beacon_core.notifications import dispatch as D
from beacon_core.notifications import senders as S


class _RecordingSession:
    """Just enough session for the delivery log (#181): collect what was added.
    `execute` asserts, because a handful of writes must never trigger a trim."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):                    # pragma: no cover - failure path
        pass

    async def execute(self, *a, **k):            # pragma: no cover - guard
        raise AssertionError("unexpected trim")

    def rows(self):
        return [o for o in self.added if isinstance(o, NotificationDelivery)]


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


def test_format_message_size_is_the_first_detail_row():
    """#179 — lot size scales the money at risk, so it leads the detail block."""
    _subj, text = D.format_message("order_filled", {
        "symbol": "XAUUSD", "direction": "BUY", "size": "0.30", "price": "3428.10"})
    rows = text.splitlines()
    assert rows[0].startswith("Size:") and "0.30" in rows[0]
    assert "Price:" in rows[1]
    # unset size simply drops the row — no "None", no error
    _s2, t2 = D.format_message("order_filled", {"symbol": "XAUUSD", "price": "3428.10"})
    assert "Size" not in t2 and "None" not in t2


def test_symbolless_headline_names_the_account():
    """#207 — the digest and the arm alarm are PER-ACCOUNT and carry no symbol.
    Three arms must not arrive under three identical headlines."""
    heads = [D.format_message("daily_summary",
                              {"account": f"acct{a} · Arm {arm}", "pl": pl,
                               "date": "2026-08-16", "wins": 3, "losses": 1})[0]
             for a, arm, pl in ((5, "A", 2410.0), (7, "B", -880.5), (8, "C", 1204.5))]
    assert len(set(heads)) == 3                       # distinguishable at a glance
    assert heads[0].startswith("📊 acct5 · Arm A — Daily summary")
    assert "P&L -880.50" in heads[1]
    # the arm alarm has neither symbol nor P&L, and still names the arm
    subj, _t = D.format_message("arm_dark", {"account": "acct7 · Arm B"})
    assert subj == "🌑 acct7 · Arm B — A/B arm not trading", subj


def test_symbol_bearing_headlines_are_unchanged_by_the_account_promotion():
    """The account is promoted ONLY when there is no symbol — a trade event that
    carries both must render exactly as it did before #207."""
    ctx = {"symbol": "XAUUSD", "direction": "SELL", "pl": -40.0,
           "account": "acct5 · Arm A", "price": "3428.10"}
    subj, text = D.format_message("trade_closed", ctx)
    assert subj == ("🏁 🔽 SELL XAUUSD — Trade closed"
                    "  |  P&L -40.00"), subj
    assert "acct5" not in subj                        # still a detail row only
    assert "Account:" in text and "acct5 · Arm A" in text


def test_format_message_default_when_no_template():
    # no templates arg -> byte-for-byte the built-in format (backward compat)
    a = D.format_message("tp_hit", {"symbol": "XAUUSD", "direction": "BUY", "pl": "5"})
    b = D.format_message("tp_hit", {"symbol": "XAUUSD", "direction": "BUY", "pl": "5"}, {})
    assert a == b
    assert a[0].startswith("🎯")


def test_format_message_custom_template_overrides():
    tmpl = {"trade_closed": {"subject": "Done {symbol} {pl}",
                             "body": "Channel {channel} at {close_time}"}}
    subj, text = D.format_message(
        "trade_closed",
        {"symbol": "XAUUSD", "pl": "+42.10", "channel": "Gold VIP",
         "close_time": "2026-07-26 14:32"},
        tmpl)
    assert subj == "Done XAUUSD +42.10"
    assert text == "Channel Gold VIP at 2026-07-26 14:32"


def test_format_message_partial_template_keeps_default_body():
    # only a custom subject -> body falls back to the default aligned block
    tmpl = {"sl_hit": {"subject": "STOP {symbol}"}}
    subj, text = D.format_message(
        "sl_hit", {"symbol": "XAUUSD", "price": "2400", "account": "Gold"}, tmpl)
    assert subj == "STOP XAUUSD"
    assert "Price:" in text and "Account:" in text        # default detail preserved


def test_format_message_template_missing_token_renders_empty():
    tmpl = {"tp_hit": {"subject": "{symbol} {nope}", "body": "{ai.verdict}"}}
    subj, text = D.format_message("tp_hit", {"symbol": "XAUUSD"}, tmpl)
    assert subj == "XAUUSD "                               # missing token -> empty
    assert text == ""


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


def test_broker_error_with_an_oversized_reason_delivers_balanced_html():
    """#180 fires broker_error carrying a raw broker rejection reason, which can
    be arbitrarily long — exactly the shape that used to be char-sliced into
    malformed HTML and silently dropped (#76). Lock in the trim-then-escape path."""
    subj, text = D.format_message("broker_error", {
        "symbol": "XAUUSD", "account": "Capital Demo",
        "detail": "Order rejected: <risk & margin> " + "why " * 3000})
    body = S.build_telegram_body(subj, text)
    assert len(body) <= S._TELEGRAM_LIMIT
    assert body.count("<pre>") == 1 and body.count("</pre>") == 1
    assert body.count("<b>") == 1 and body.count("</b>") == 1
    assert body.endswith("</pre>")                       # not cut mid-tag
    assert "&lt;risk &amp; margin&gt;" in body           # whole entities, escaped
    assert "…(truncated)" in body


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


def test_notify_records_every_dispatch_and_what_each_channel_did():
    """#181 — the per-channel outcome used to be logged and thrown away, so
    "did my last alert reach Telegram?" had no answer. It lands as a row now."""
    async def fake(cfg, subject, text):
        pass

    async def fake_get_setting(session, key, default=None):
        return {
            "channels": {"telegram": {"enabled": True, "bot_token_enc": "t", "chat_id": "1"},
                         "sms": {"enabled": False}},
            "routing": {"tp_hit": ["telegram", "sms"]},     # new_signal routed nowhere
        }

    orig_get, orig_senders = D.get_setting, dict(S.SENDERS)
    D.get_setting = fake_get_setting
    S.SENDERS["telegram"] = fake
    DL._writes_since_trim = 0
    try:
        sess = _RecordingSession()
        asyncio.run(D.notify(sess, "tp_hit", {"symbol": "XAUUSD", "pl": "12.5"}))
        row, = sess.rows()
        assert row.event_id == "tp_hit"
        assert row.results == {"telegram": "ok", "sms": "disabled"}
        assert row.ok is True                       # at least one channel delivered
        assert "XAUUSD" in row.subject

        # an event routed nowhere still lands a row — "nothing was routed" is
        # the answer to half the "why didn't I get an alert?" questions.
        sess2 = _RecordingSession()
        asyncio.run(D.notify(sess2, "new_signal", {"symbol": "XAUUSD"}))
        row2, = sess2.rows()
        assert row2.results == {} and row2.ok is False
    finally:
        D.get_setting = orig_get
        S.SENDERS.clear()
        S.SENDERS.update(orig_senders)
        DL._writes_since_trim = 0


def test_delivery_log_write_never_breaks_dispatch():
    """Telemetry must never be the reason a notification (or a trade) misbehaves:
    a session that throws on every call leaves the dispatch result intact."""
    class _Broken:
        def add(self, obj):
            raise RuntimeError("db is down")

        async def commit(self):
            raise RuntimeError("db is down")

        async def rollback(self):
            raise RuntimeError("still down")

    async def fake(cfg, subject, text):
        pass

    async def fake_get_setting(session, key, default=None):
        return {"channels": {"telegram": {"enabled": True, "bot_token_enc": "t", "chat_id": "1"}},
                "routing": {"tp_hit": ["telegram"]}}

    orig_get, orig_senders = D.get_setting, dict(S.SENDERS)
    D.get_setting = fake_get_setting
    S.SENDERS["telegram"] = fake
    try:
        res = asyncio.run(D.notify(_Broken(), "tp_hit", {"symbol": "XAUUSD"}))
    finally:
        D.get_setting = orig_get
        S.SENDERS.clear()
        S.SENDERS.update(orig_senders)
    assert res["results"] == {"telegram": "ok"}     # the send still happened


def test_delivery_to_dict_shape():
    import datetime as dt
    row = NotificationDelivery(
        id=7, event_id="sl_hit", subject="🔴 SELL XAUUSD — Stop-loss hit",
        results={"telegram": "error: Telegram API 400"}, ok=False,
        created_at=dt.datetime(2026, 7, 31, 9, 30, tzinfo=dt.timezone.utc))
    d = DL.to_dict(row)
    assert d["event"] == "sl_hit"
    assert d["label"] == "Stop-loss hit"            # resolved from the catalog
    assert d["ok"] is False
    assert d["results"]["telegram"].startswith("error:")
    assert d["ts"].startswith("2026-07-31T09:30")


def test_send_test_unbuilt_channel():
    res = asyncio.run(D.send_test(None, "whatsapp"))   # no sender -> friendly error
    assert res["ok"] is False and "not built" in res["error"].lower()


def test_render_event_applies_stored_template():
    async def fake_get_setting(session, key, default=None):
        return {"templates": {"trade_closed": {"subject": "Closed {symbol} {pl}"}}}

    orig = D.get_setting
    D.get_setting = fake_get_setting
    try:
        subj, _ = asyncio.run(D.render_event(None, "trade_closed",
                                             {"symbol": "XAUUSD", "pl": "+42.10"}))
    finally:
        D.get_setting = orig
    assert subj == "Closed XAUUSD +42.10"


def test_send_event_to_channel_renders_and_reports_status():
    sent = []

    async def fake(cfg, subject, text):
        sent.append((subject, text))

    async def fake_get_setting(session, key, default=None):
        return {
            "channels": {"telegram": {"enabled": True, "bot_token_enc": "t", "chat_id": "1"},
                         "sms": {"enabled": False}},
            "templates": {"tp_hit": {"body": "hit {price}"}},
        }

    orig_get, orig_senders = D.get_setting, dict(S.SENDERS)
    D.get_setting = fake_get_setting
    S.SENDERS["telegram"] = fake
    try:
        ok = asyncio.run(D.send_event_to_channel(None, "tp_hit", {"price": "2400"}, "telegram"))
        disabled = asyncio.run(D.send_event_to_channel(None, "tp_hit", {}, "sms"))
        nosender = asyncio.run(D.send_event_to_channel(None, "tp_hit", {}, "push"))
    finally:
        D.get_setting = orig_get
        S.SENDERS.clear()
        S.SENDERS.update(orig_senders)
    assert ok == "ok" and len(sent) == 1
    assert sent[0][1] == "hit 2400"                 # body rendered from the template
    assert "Take-profit hit" in sent[0][0]          # subject fell back to default
    assert disabled == "disabled"          # routed-agnostic, but channel must be enabled
    assert nosender == "no_sender"


def test_resolve_channel_passes_plaintext_and_defaults():
    stored = {"channels": {"email": {"enabled": True, "smtp_host": "smtp.x", "smtp_port": 587,
                                     "use_tls": True, "smtp_password_enc": "plaintext-passthrough"}}}
    cfg = D.resolve_channel("email", stored)
    # decrypt() passes through non-`enc:v1:` values unchanged
    assert cfg["smtp_host"] == "smtp.x" and cfg["smtp_port"] == 587
    assert cfg["smtp_password"] == "plaintext-passthrough" and cfg["enabled"] is True
