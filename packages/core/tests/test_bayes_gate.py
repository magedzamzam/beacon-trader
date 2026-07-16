"""Learned-P(win) execution gate (#64) — pure decision + guardrails."""
from beacon_core.execution import bayes_gate as BG
from beacon_core.analysis import bayes as B


def _cfg(**over):
    return BG.gate_cfg({**over})


def _score(p, lo, hi, n=100):
    return {"p_win": p, "ci_low": lo, "ci_high": hi, "n": n}


def test_defaults_are_shadow_first():
    cfg = BG.gate_cfg(None)
    assert cfg["enabled"] is False and cfg["mode"] == "log_only"
    assert BG.acts_live(cfg) is False                 # never acts by default


def test_acts_live_requires_enabled_and_active():
    assert BG.acts_live(_cfg(enabled=True, mode="active")) is True
    assert BG.acts_live(_cfg(enabled=True, mode="log_only")) is False   # shadow
    assert BG.acts_live(_cfg(enabled=False, mode="active")) is False


def test_skip_only_when_upper_bound_poor():
    cfg = _cfg(skip_threshold=0.40, desize_threshold=0.50)
    # even the optimistic bound below skip_threshold -> skip
    assert BG.decide(cfg, _score(0.3, 0.2, 0.38))[0] == "skip"
    # upper bound above skip_threshold -> not skipped (a wide CI isn't punished on
    # the point estimate alone)
    assert BG.decide(cfg, _score(0.3, 0.2, 0.45))[0] == "allow"


def test_desize_band_and_full():
    cfg = _cfg(skip_threshold=0.40, desize_threshold=0.50, desize_factor=0.5)
    a, f, r = BG.decide(cfg, _score(0.45, 0.42, 0.48))     # ci_high in [0.40,0.50)
    assert a == "allow" and f == 0.5 and r == "p_win_mid"
    a, f, r = BG.decide(cfg, _score(0.7, 0.62, 0.78))       # ci_high >= 0.50
    assert a == "allow" and f == 1.0 and r == "p_win_ok"


def test_desize_factor_zero_becomes_skip():
    cfg = _cfg(desize_factor=0.0)
    assert BG.decide(cfg, _score(0.45, 0.42, 0.48))[0] == "skip"


def test_significance_guardrail_observe_only():
    cfg = _cfg(min_trades=30)
    a, f, r = BG.decide(cfg, _score(0.2, 0.1, 0.25, n=10))   # n below floor
    assert a == "allow" and f == 1.0 and r == "observe_insufficient_n"
    assert BG.decide(cfg, None)[2] == "observe_no_score"


def test_wide_ci_guardrail_observe_only():
    cfg = _cfg(max_ci_width=0.30, skip_threshold=0.40)
    # ci_high 0.38 < skip_threshold but the interval is too wide -> observe, not skip
    a, f, r = BG.decide(cfg, _score(0.25, 0.05, 0.38))
    assert a == "allow" and r == "observe_wide_ci"


def test_score_interval_widens_with_thin_evidence():
    lo_thin, hi_thin = B.score_interval(0.5, 5, 0.5)
    lo_thick, hi_thick = B.score_interval(0.5, 500, 0.5)
    assert (hi_thin - lo_thin) > (hi_thick - lo_thick)      # less evidence -> wider CI


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
