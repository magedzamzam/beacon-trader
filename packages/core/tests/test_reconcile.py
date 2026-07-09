"""Outcome parsing (#23) + reconciliation categories (#24)."""
from beacon_core.parsing.outcomes import parse_outcome
from beacon_core.analysis.reconcile import reconcile_signal


# ---- parse_outcome ----
def test_tp_hit_variants():
    assert parse_outcome("#XAUUSD TP2HIT 60PIPS PROFIT")["max_tp"] == 2
    assert parse_outcome("TP1 hit")["tp_hits"] == [1]
    assert parse_outcome("#XAUUSD TP3HIT 100PIPS PROFIT")["max_tp"] == 3
    assert parse_outcome("TP² hit ✅")["tp_hits"] == [2]          # superscript
    assert parse_outcome("tp4 smashed 🎯")["max_tp"] == 4


def test_sl_and_all_tp():
    assert parse_outcome("SL HIT 80 PIPS")["sl_hit"] is True
    assert parse_outcome("stopped out")["sl_hit"] is True
    o = parse_outcome("All TP done ✅")
    assert o["all_tp"] is True


def test_generic_and_non_outcomes():
    g = parse_outcome("Take profit hit 🎯")
    assert g["tp_generic"] is True and g["max_tp"] == 0
    assert parse_outcome("Good morning traders, watch 4020") is None
    assert parse_outcome("") is None
    # a signal-looking line is not an outcome (no hit word near the TP levels)
    assert parse_outcome("XAUUSD BUY 4015 TP1 4018 TP2 4021 SL 4005") is None


# ---- reconcile_signal ----
def _leg(tp, status, outcome=None, fill=None):
    return {"tp_index": tp, "status": status, "outcome": outcome, "fill_price": fill}


def test_match():
    legs = [_leg(1, "closed", "tp_hit", 4015), _leg(2, "closed", "tp_hit", 4015),
            _leg(3, "closed", "tp_hit", 4015)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[{"max_tp_claimed": 3, "sl_claimed": False, "all_tp": False}], legs=legs)
    assert r["category"] == "match" and r["bot_max_tp"] == 3


def test_no_fill():
    legs = [_leg(i, "cancelled", "cancelled") for i in (1, 2, 3)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[{"max_tp_claimed": 3, "sl_claimed": False, "all_tp": False}], legs=legs)
    assert r["category"] == "no_fill" and r["bot_any_fill"] is False


def test_shortfall_stopped_before_tp():
    legs = [_leg(1, "closed", "tp_hit", 4015), _leg(2, "closed", "sl_hit", 4015),
            _leg(3, "closed", "sl_hit", 4015)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[{"max_tp_claimed": 3, "sl_claimed": False, "all_tp": False}], legs=legs)
    assert r["category"] == "shortfall_stopped_before_tp"


def test_shortfall_leg_missing():
    legs = [_leg(1, "closed", "tp_hit", 4015)]         # only TP1 exists, channel claims TP5
    r = reconcile_signal(signal_status="executed", n_signal_tps=5, is_history=False,
                         claims=[{"max_tp_claimed": 5, "sl_claimed": False, "all_tp": False}], legs=legs)
    assert r["category"] == "shortfall_leg_missing"


def test_executed_no_trade_and_not_executed():
    claim = [{"max_tp_claimed": 1, "sl_claimed": False, "all_tp": False}]
    assert reconcile_signal(signal_status="executed", n_signal_tps=1, is_history=False,
                            claims=claim, legs=[])["category"] == "executed_no_trade"
    assert reconcile_signal(signal_status="blocked", n_signal_tps=1, is_history=False,
                            claims=claim, legs=[])["category"] == "not_executed"


def test_all_tp_resolves_to_signal_tp_count():
    legs = [_leg(1, "closed", "tp_hit", 4015), _leg(2, "closed", "tp_hit", 4015)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=2, is_history=False,
                         claims=[{"max_tp_claimed": 0, "sl_claimed": False, "all_tp": True}], legs=legs)
    assert r["claimed_max_tp"] == 2 and r["category"] == "match"


def test_claim_sl_only():
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[{"max_tp_claimed": 0, "sl_claimed": True, "all_tp": False}],
                         legs=[_leg(1, "closed", "sl_hit", 4015)])
    assert r["category"] == "claim_sl"
