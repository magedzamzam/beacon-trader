"""Payoff-geometry A/B roll-up (#80 item 3 / #85 action 2): R-multiple
normalization, payoff ratio, breakeven-leg rate, winners-reach-TP3 — the pure
math behind execution_geometry_ab_report, tested DB-free."""
from beacon_core.analysis.report import geometry_ab_rollup


def _t(tid, acct, pl, risk, label=None):
    return {"trade_id": tid, "account_id": acct, "account": f"acct{acct}",
            "realized_pl": pl, "planned_risk": risk, "strategy_label": label}


def _l(tid, outcome, tp_index):
    return {"trade_id": tid, "outcome": outcome, "tp_index": tp_index}


def _arm(rep, acct_id):
    return next(a for a in rep["by_arm"] if a["account_id"] == acct_id)


def test_r_multiple_is_scale_free():
    """Same R geometry at very different nominal sizes -> identical R stats.
    This is the equity-parity confound fix (#85 §2): a drawn-down arm sized in
    small AED and a fresh arm sized large both normalize to the same R."""
    # Arm A: win +2R (100 on 50 risk), loss -1R (-30 on 30 risk).
    # Arm B: SAME R geometry but 10x nominal — win +2R (1000/500), loss -1R (-300/300).
    trades = [_t(1, 5, 100, 50), _t(2, 5, -30, 30),
              _t(3, 7, 1000, 500), _t(4, 7, -300, 300)]
    rep = geometry_ab_rollup(trades, [])
    a, b = _arm(rep, 5), _arm(rep, 7)
    assert a["avg_R"] == b["avg_R"] == 0.5          # (2 + -1)/2
    assert a["avg_win_R"] == b["avg_win_R"] == 2.0
    assert a["avg_loss_R"] == b["avg_loss_R"] == -1.0
    assert a["payoff_ratio"] == b["payoff_ratio"] == 2.0
    assert a["profit_factor"] == b["profit_factor"] == 2.0   # 2R / 1R
    # Nominal net differs 10x — the raw-AED incomparability R removes.
    assert a["net_nominal"] == 70.0 and b["net_nominal"] == 700.0


def test_win_rate_and_counts():
    trades = [_t(1, 5, 10, 5), _t(2, 5, 20, 5), _t(3, 5, -5, 5), _t(4, 5, -5, 5)]
    a = _arm(geometry_ab_rollup(trades, []), 5)
    assert a["n_trades"] == 4
    assert a["win_rate"] == 0.5
    assert a["n_with_risk"] == 4 and a["n_no_risk"] == 0


def test_missing_risk_excluded_from_R_but_counted_for_win():
    """planned_risk None/0 -> R undefined: still counts for win-rate/legs, but is
    kept out of the R sums and tallied in n_no_risk."""
    trades = [_t(1, 5, 100, 50), _t(2, 5, 40, None), _t(3, 5, 10, 0)]
    a = _arm(geometry_ab_rollup(trades, []), 5)
    assert a["n_trades"] == 3 and a["win_rate"] == 1.0
    assert a["n_with_risk"] == 1 and a["n_no_risk"] == 2
    assert a["avg_R"] == 2.0                         # only the one with real risk


def test_breakeven_leg_rate_and_winners_reach_tp3():
    # One winning trade (tid 1) that ran to TP3; one winning trade (tid 2) cut at
    # TP1 with two breakeven legs (the ratchet strangle #80 describes).
    trades = [_t(1, 7, 300, 100), _t(2, 7, 20, 100)]
    legs = [
        _l(1, "tp_hit", 1), _l(1, "tp_hit", 2), _l(1, "tp_hit", 3),   # ran to TP3
        _l(2, "tp_hit", 1), _l(2, "breakeven", 2), _l(2, "breakeven", 3),
    ]
    a = _arm(geometry_ab_rollup(trades, legs), 7)
    assert a["n_legs"] == 6 and a["n_breakeven_legs"] == 2
    assert a["breakeven_leg_rate"] == round(2 / 6, 4)
    # Only tid 1 reached >=TP3 among the 2 winners.
    assert a["pct_winners_reach_tp3"] == 0.5


def test_tp3_requires_winning_trade():
    """A tp_hit leg at >=TP3 on a LOSING trade does not count as a winner-ran."""
    trades = [_t(1, 5, -50, 50)]                     # net loss
    legs = [_l(1, "tp_hit", 3), _l(1, "sl_hit", 4)]
    a = _arm(geometry_ab_rollup(trades, legs), 5)
    assert a["win_rate"] == 0.0                       # the trade lost
    assert a["pct_winners_reach_tp3"] is None         # no winners to divide by


def test_arms_labels_and_separation():
    trades = [_t(1, 5, 10, 5, "TestA: BE@TP1"), _t(2, 7, 10, 5, "TestB: BE@TP2")]
    rep = geometry_ab_rollup(trades, [], source_id=12)
    assert rep["source_id"] == 12
    assert _arm(rep, 5)["arms"] == ["TestA: BE@TP1"]
    assert _arm(rep, 7)["arms"] == ["TestB: BE@TP2"]
    # arms sorted by account id
    assert [a["account_id"] for a in rep["by_arm"]] == [5, 7]


def test_profit_factor_none_when_no_losers():
    trades = [_t(1, 5, 10, 5), _t(2, 5, 20, 5)]
    a = _arm(geometry_ab_rollup(trades, []), 5)
    assert a["profit_factor"] is None                # no losing R to divide by
    assert a["payoff_ratio"] is None
    assert a["avg_loss_R"] is None


def test_empty():
    rep = geometry_ab_rollup([], [])
    assert rep["n_closed"] == 0 and rep["by_arm"] == []
