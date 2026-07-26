"""Template renderer + field-descriptor (#139). Pure — no DB/crypto/network."""
from beacon_core.notifications import templates as T
from beacon_core.notifications import config as N


def test_render_substitutes_flat_tokens():
    out = T.render("{direction} {symbol} @ {entry}",
                   {"direction": "BUY", "symbol": "XAUUSD", "entry": "3421.5"})
    assert out == "BUY XAUUSD @ 3421.5"


def test_render_dotted_path():
    out = T.render("AI says {ai.verdict} ({ai.confidence})",
                   {"ai": {"verdict": "approve", "confidence": "0.82"}})
    assert out == "AI says approve (0.82)"


def test_render_missing_token_is_empty_never_raises():
    # unset key, and a dotted path whose parent is absent -> both render empty
    out = T.render("[{missing}][{ai.verdict}]", {"symbol": "XAUUSD"})
    assert out == "[][]"


def test_render_none_and_empty_values_render_empty():
    out = T.render("<{a}|{b}|{c}>", {"a": None, "b": "", "c": 0})
    assert out == "<||0>"                     # 0 is a real value, not "empty"


def test_render_injection_is_inert():
    # the classic str.format attack: attribute access must NOT happen.
    ctx = {"x": "hi"}
    assert T.render("{x.__class__}", ctx) == ""          # no attribute walk
    assert T.render("{0}", ctx) == ""                    # positional -> literal miss
    # a token that walks into a non-dict value stops safely
    assert T.render("{x.y}", ctx) == ""


def test_render_leaves_unknown_braces_untouched():
    # not a valid token (space/punctuation) -> literal passthrough
    assert T.render("keep {this literal} and { spaced }", {}) == \
        "keep {this literal} and { spaced }"


def test_render_empty_template():
    assert T.render("", {"a": "b"}) == ""
    assert T.render(None, {"a": "b"}) == ""


def test_sample_ctx_nests_dotted_fields():
    s = T.sample_ctx()
    assert s["symbol"] == "XAUUSD"
    assert isinstance(s["ai"], dict) and s["ai"]["verdict"] == "approve"


def test_field_descriptor_matches_sample_and_renderer():
    """The picker can never advertise a token the renderer can't resolve: every
    descriptor token resolves against sample_ctx to exactly its advertised
    example."""
    desc = T.field_descriptor()
    sample = T.sample_ctx()
    assert desc, "descriptor is non-empty"
    for f in desc:
        assert set(f) >= {"token", "label", "example"}
        assert T.render("{" + f["token"] + "}", sample) == f["example"]


def test_event_fields_are_all_known_tokens():
    known = {name for name, _l, _e in T.FIELDS}
    for ev, toks in T.EVENT_FIELDS.items():
        for tok in toks:
            assert tok in known, (ev, tok)          # no contract invents a token
    # every routing-matrix event has a contract entry (even if empty)
    assert set(T.EVENT_FIELDS) == set(N.EVENT_IDS)


def test_field_descriptor_is_per_event_and_honest():
    d = T.field_descriptor("trade_closed")
    tokens = [f["token"] for f in d]
    assert tokens == ["symbol", "direction", "pl", "open_time", "close_time"]  # FIELDS order
    # trade_closed does NOT carry channel/account/entry/price -> not advertised
    assert "channel" not in tokens and "account" not in tokens and "price" not in tokens
    # new_signal is a different, disjoint-ish set
    ns = {f["token"] for f in T.field_descriptor("new_signal")}
    assert ns == {"symbol", "direction", "entry", "sl", "tp", "channel"}


def test_non_emitted_event_has_no_fields():
    assert T.field_descriptor("daily_summary") == []
    assert T.sample_ctx("daily_summary") == {}
    assert T.is_emitted("daily_summary") is False
    assert T.is_emitted("trade_closed") is True


def test_per_event_sample_matches_descriptor():
    """Preview parity, per event: every advertised token resolves against that
    event's sample to exactly its example."""
    for ev in N.EVENT_IDS:
        sample = T.sample_ctx(ev)
        for f in T.field_descriptor(ev):
            assert T.render("{" + f["token"] + "}", sample) == f["example"]
        # the sample carries ONLY the event's roots — nothing extra
        roots = {t.split(".")[0] for t in T.EVENT_FIELDS[ev]}
        assert set(sample) == roots


def test_no_arg_descriptor_is_full_set_backcompat():
    assert len(T.field_descriptor()) == len(T.FIELDS)
    assert "ai" in T.sample_ctx()                    # full set still nests ai.*


def test_sanitize_templates_keeps_known_events_and_nonempty_parts():
    raw = {
        "trade_closed": {"subject": "Closed {symbol}", "body": ""},   # empty body dropped
        "tp_hit": {"subject": "", "body": "  "},                       # only whitespace body kept
        "new_signal": {"subject": "", "body": ""},                     # all empty -> event dropped
        "bogus_event": {"subject": "x", "body": "y"},                  # unknown -> dropped
        "sl_hit": {"body": 123},                                       # non-str -> dropped
    }
    out = T.sanitize_templates(raw, N.EVENT_IDS)
    assert out["trade_closed"] == {"subject": "Closed {symbol}"}
    assert out["tp_hit"] == {"body": "  "}     # whitespace is a real (non-"") string
    assert "new_signal" not in out
    assert "bogus_event" not in out
    assert "sl_hit" not in out
