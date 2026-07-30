"""Outcome parsing (#23) + reconciliation categories (#24, #136, #172)."""
from beacon_core.parsing.outcomes import parse_outcome
from beacon_core.analysis import reconcile as R
from beacon_core.analysis.reconcile import (reconcile_signal, override_to_claim,
                                            valid_override, is_protected,
                                            GAP_CATEGORIES, PROTECTED_CATEGORIES)


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


def test_no_claim_when_the_channel_never_posted_an_outcome():
    """#172: the bot filled, the channel went quiet. Nothing to compare against —
    neither a match nor a gap. These were structurally invisible because the
    Reconciler listed claims and worked backwards, and they turned out to be
    where the losses live (claimed 65% win / +20k, unclaimed 33% / -207k)."""
    legs = [_leg(1, "closed", "tp_hit", 4015), _leg(2, "closed", "sl_hit", 4015)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[], legs=legs)
    assert r["category"] == "no_claim"
    assert r["claimed_max_tp"] == 0 and r["bot_any_fill"] is True


def test_no_claim_is_not_claim_sl():
    """The regression this branch exists to prevent. An unclaimed signal and an
    SL-claiming one BOTH have claimed_max_tp == 0, so without the `no_claim`
    branch, opening the query to traded signals would have dumped 38% of the book
    into `claim_sl` and quietly corrupted it."""
    legs = [_leg(1, "closed", "sl_hit", 4015)]
    silent = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                              claims=[], legs=legs)
    reported = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                                claims=[{"max_tp_claimed": 0, "sl_claimed": True,
                                         "all_tp": False}], legs=legs)
    assert silent["category"] == "no_claim"
    assert reported["category"] == "claim_sl"


def test_no_claim_is_uncomparable_but_not_protected_or_a_gap():
    """It must stay OUT of the match-rate denominator (nothing to score against)
    without being mistaken for protection (the bot did trade) or for a shortfall
    (we do not know that it fell short)."""
    assert R.is_uncomparable("no_claim") is True
    assert R.is_protected("no_claim") is False
    assert R.is_match("no_claim") is False
    assert "no_claim" not in R.GAP_CATEGORIES
    assert R.is_uncomparable("match") is False


def test_leg_and_fill_checks_still_outrank_no_claim():
    """#136's precedence stands: 'did the bot place legs / did any fill' is more
    fundamental than anything about the claim, including its absence."""
    unfilled = [_leg(i, "cancelled", "cancelled") for i in (1, 2)]
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[], legs=unfilled)
    assert r["category"] == "no_fill"
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[], legs=[])
    assert r["category"] == "executed_no_trade"
    r = reconcile_signal(signal_status="rejected", n_signal_tps=3, is_history=False,
                         claims=[], legs=[])
    assert r["category"] == "not_executed"


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
    # executed + zero legs + NO block on record -> genuine "said executed, placed nothing" bug
    assert reconcile_signal(signal_status="executed", n_signal_tps=1, is_history=False,
                            claims=claim, legs=[])["category"] == "executed_no_trade"
    # not-executed status -> protection
    assert reconcile_signal(signal_status="blocked", n_signal_tps=1, is_history=False,
                            claims=claim, legs=[])["category"] == "not_executed"


def test_executed_but_blocked_is_protected():
    """#136 pt2: a risk-blocked signal ends 'executed' with zero legs, but with a
    block on record it's PROTECTION (not_executed), not the executed_no_trade bug."""
    claim = [{"max_tp_claimed": 2, "sl_claimed": False, "all_tp": False}]
    r = reconcile_signal(signal_status="executed", n_signal_tps=2, is_history=False,
                         claims=claim, legs=[], blocked=True)
    assert r["category"] == "not_executed"
    assert is_protected(r["category"])
    assert "not_executed" in PROTECTED_CATEGORIES and "not_executed" not in GAP_CATEGORIES


def test_no_fill_takes_precedence_over_claim_sl():
    """#136 pt4: legs were PLACED but none FILLED -> no_fill, even when the channel
    only claimed an SL (no TP). The old ordering hid these as claim_sl, which is why
    No-Fill went empty for weeks despite real zero-fill trades (#249/#250)."""
    legs = [_leg(i, "cancelled", "cancelled") for i in (1, 2, 3, 4)]   # 4 legs, 0 fills
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[{"max_tp_claimed": 0, "sl_claimed": True, "all_tp": False}],
                         legs=legs)
    assert r["category"] == "no_fill" and r["bot_any_fill"] is False
    # and the TP-claimed variant still classifies as no_fill
    r2 = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                          claims=[{"max_tp_claimed": 3, "sl_claimed": False, "all_tp": False}],
                          legs=legs)
    assert r2["category"] == "no_fill"


def test_override_to_claim_mapping():
    assert override_to_claim(None, 3) is None
    assert override_to_claim("none", 3) is None
    assert override_to_claim("sl_hit", 3) == {"max_tp_claimed": 0, "sl_claimed": True, "all_tp": False}
    assert override_to_claim("tp2", 3)["max_tp_claimed"] == 2
    assert override_to_claim("all_tp", 3) == {"max_tp_claimed": 3, "sl_claimed": False, "all_tp": True}
    assert override_to_claim("breakeven", 3) == {"max_tp_claimed": 0, "sl_claimed": False, "all_tp": False}
    assert override_to_claim("garbage", 3) is None
    assert valid_override(None) and valid_override("none") and valid_override("tp5")
    assert valid_override("sl_hit") and not valid_override("tpX") and not valid_override("nope")


def test_override_flips_reconciliation():
    """A misparsed 'REVERSED AND HIT OUR RISK' (parsed as TP) can be force-tagged SL:
    with the override applied to the claim, a filled+stopped signal reads claim_sl."""
    legs = [_leg(1, "closed", "sl_hit", 4015)]
    ov = override_to_claim("sl_hit", 3)
    r = reconcile_signal(signal_status="executed", n_signal_tps=3, is_history=False,
                         claims=[ov], legs=legs)
    assert r["category"] == "claim_sl"


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
