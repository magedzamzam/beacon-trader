"""Confirmation-staged entry engine (#129) — pure, no DB/broker/clock.

Covers the partition (by TP tier), the break-distance clamps, and the DECIDE
engine over the four synthetic price paths the acceptance criteria name:
straight-to-SL, pullback-continue, V-bounce, fakeout-reclaim — plus the
conservative-only invariant of the modifier-decider pipeline."""
import pytest

from beacon_core.execution import staging as S
from beacon_core.execution.staging import StagingContext as C, DEFAULT_STAGED as D


# ---- geometry ----
def test_zone_edges_direction():
    assert S.zone_edges("BUY", 4180, 4176) == (4180.0, 4176.0)   # near=high, deep=low
    assert S.zone_edges("SELL", 4180, 4176) == (4176.0, 4180.0)  # near=low, deep=high


def test_beyond_deep():
    assert S.beyond_deep("BUY", 4171, 4176) == 5.0     # 5 below deep
    assert S.beyond_deep("BUY", 4178, 4176) == 0.0     # not beyond
    assert S.beyond_deep("SELL", 4183, 4180) == 3.0    # 3 above deep


# ---- partition: the quant's concrete cases ----
def test_partition_by_tp_tier():
    p3 = S.partition_tps([1, 2, 3], D)                 # 3-TP single: 1/1/1
    assert p3 == {"toe_in": [1], "runner": [3], "reclaim": [2]}
    p5 = S.partition_tps([1, 2, 3, 4, 5], D)           # 5-TP: 1/1/3 (deferred hits 60% cap)
    assert p5 == {"toe_in": [1], "runner": [5], "reclaim": [2, 3, 4]}
    p2 = S.partition_tps([1, 2], D)                    # 2-TP: toe + deferred, no runner
    assert p2 == {"toe_in": [1], "runner": [], "reclaim": [2]}
    p1 = S.partition_tps([1], D)                       # 1-TP: all toe-in
    assert p1 == {"toe_in": [1], "runner": [], "reclaim": []}


def test_partition_max_deferred_cap():
    # 6-TP at 60% cap -> at most 3 deferred; the two nearest middles fall to runner
    p6 = S.partition_tps([1, 2, 3, 4, 5, 6], D)
    assert len(p6["reclaim"]) <= int(0.60 * 6)
    assert set(p6["toe_in"] + p6["runner"] + p6["reclaim"]) == {1, 2, 3, 4, 5, 6}  # covers all
    assert p6["toe_in"] == [1] and 6 in p6["runner"]


# ---- break distance + clamps ----
def test_break_distance_clamps():
    # 0.35*14 = 4.9, but clamped to 0.5*stop(8)=4.0
    assert S.break_distance(D, atr=14.0, sl_dist=8.0) == 4.0
    # loose stop -> ATR term wins, but abs cap 8.0 bites at high ATR
    assert S.break_distance(D, atr=40.0, sl_dist=100.0) == 8.0     # 0.35*40=14 -> cap 8
    # No ATR -> the cash fallback, which is frozen at 0 (#250) and therefore off:
    # break_distance returns None and the reclaim tranche never arms without a
    # defined break. That was already the default; #250 removed the ability to
    # set it, not the behaviour.
    assert S.break_distance(D, atr=None, sl_dist=8.0) is None
    assert S.break_distance({**D, "reclaim_break_cash": 3.0}, atr=None, sl_dist=100.0) is None


# ---- shared geometry for the DECIDE paths (BUY, zone 4180/4176, SL 4168, ATR 14) ----
NEAR, DEEP, SL, ATR = 4180.0, 4176.0, 4168.0, 14.0     # break_distance -> 4.0 (0.5*8)


def _ctx(price, beyond=0.0):
    return C(direction="BUY", near_edge=NEAR, deep_edge=DEEP, sl=SL, price=price,
             atr=ATR, max_adverse_beyond_deep=beyond)


def _dec(role, state, ctx, minutes=0.0):
    return S.decide_tranche(role=role, state=state, ctx=ctx, cfg=D, minutes_in_state=minutes)


# ---- RUNNER tranche ----
def test_runner_deploys_at_deep_edge():
    assert _dec(S.RUNNER, S.PENDING, _ctx(4178)).action == S.WAIT        # above deep
    # #140: at/through the deep edge -> MARKET (a LIMIT here is a limit-at-market
    # the broker rejects). Holds when price is already past the deep edge too.
    d = _dec(S.RUNNER, S.PENDING, _ctx(4176))                            # at deep
    assert d.action == S.DEPLOY and d.mode == S.MODE_MARKET and d.level == DEEP
    d2 = _dec(S.RUNNER, S.PENDING, _ctx(4174))                           # already below deep
    assert d2.action == S.DEPLOY and d2.mode == S.MODE_MARKET


def test_runner_expires_if_deep_never_touched():   # V-bounce: never tags deep
    assert _dec(S.RUNNER, S.PENDING, _ctx(4179), minutes=50).action == S.EXPIRE  # > 45 ttl
    assert _dec(S.RUNNER, S.PENDING, _ctx(4179), minutes=10).action == S.WAIT


# ---- RECLAIM tranche ----
def test_reclaim_arms_on_break_beyond_deep():
    assert _dec(S.RECLAIM, S.PENDING, _ctx(4174, beyond=2.0)).action == S.WAIT   # 2 < 4
    d = _dec(S.RECLAIM, S.PENDING, _ctx(4171, beyond=5.0))                        # 5 >= 4 -> arm
    assert d.action == S.DEPLOY and d.mode == S.MODE_STOP
    assert d.level == DEEP + 0.10 * ATR                                          # stop offset above deep


def test_reclaim_pending_expires_without_break():   # pullback-continue / straight up, no break
    assert _dec(S.RECLAIM, S.PENDING, _ctx(4177), minutes=70).action == S.EXPIRE  # > 60
    assert _dec(S.RECLAIM, S.PENDING, _ctx(4177), minutes=10).action == S.WAIT


def test_reclaim_armed_waits_then_expires():        # fakeout: armed, never reclaimed
    assert _dec(S.RECLAIM, S.ARMED, _ctx(4170), minutes=30).action == S.WAIT
    assert _dec(S.RECLAIM, S.ARMED, _ctx(4170), minutes=61).action == S.EXPIRE


def test_toe_in_is_not_decided_here():
    assert _dec(S.TOE_IN, S.PENDING, _ctx(4180)).action == S.WAIT   # executor deploys T1 at entry


# ---- SELL mirror ----
def test_sell_geometry_mirrors():
    near, deep, sl = 4176.0, 4180.0, 4188.0
    ctx = C(direction="SELL", near_edge=near, deep_edge=deep, sl=sl, price=4180.0, atr=ATR)
    rd = S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=ctx, cfg=D)
    assert rd.action == S.DEPLOY and rd.mode == S.MODE_MARKET   # #140: MARKET at deep, not LIMIT
    armed = C(direction="SELL", near_edge=near, deep_edge=deep, sl=sl, price=4185.0,
              atr=ATR, max_adverse_beyond_deep=5.0)
    d = S.decide_tranche(role=S.RECLAIM, state=S.PENDING, ctx=armed, cfg=D)
    assert d.action == S.DEPLOY and d.mode == S.MODE_STOP and d.level == deep - 0.10 * ATR


# ---- modifier deciders: conservative-only invariant ----
def test_modifier_can_veto_a_deploy():
    veto = lambda **k: {"skip": True, "reason": "counter-trend"} if k["role"] == S.RUNNER else None
    d = S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4176), cfg=D, deciders=[veto])
    assert d.action == S.SKIP and "counter-trend" in d.reason


def test_modifier_can_only_shrink_size():
    shrink = lambda **k: {"size_factor": 0.5}
    d = S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4176), cfg=D, deciders=[shrink])
    assert d.action == S.DEPLOY and d.size_factor == 0.5
    # a modifier can NEVER up-size or force a deploy
    upsize = lambda **k: {"size_factor": 2.0}
    d2 = S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4176), cfg=D, deciders=[upsize])
    assert d2.size_factor == 1.0
    force = lambda **k: {"skip": False, "size_factor": 1.0}
    d3 = S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4179), cfg=D, deciders=[force])
    assert d3.action == S.WAIT            # can't turn a WAIT into a DEPLOY


# ---- staged leg planner (#129 Phase 3) ----
def test_build_staged_legs_buy():
    legs = S.build_staged_legs(direction="BUY", tps=[4185, 4190, 4195], near_edge=4180,
                               deep_edge=4176, sl=4168, atr=14, current_price=4182, cfg=D)
    by_tp = {l.tp_index: l for l in legs}
    assert by_tp[1].tranche == "toe_in" and by_tp[1].order_type == "LIMIT" and float(by_tp[1].entry) == 4180
    assert by_tp[3].tranche == "runner" and by_tp[3].order_type == "LIMIT" and float(by_tp[3].entry) == 4176
    assert by_tp[2].tranche == "reclaim" and by_tp[2].order_type == "STOP"
    assert float(by_tp[2].entry) == 4176 + 0.10 * 14 and float(by_tp[2].trigger) == 4176 + 0.10 * 14
    assert all(l.valid for l in legs)


def test_build_staged_legs_toe_in_market_when_crossed():
    legs = S.build_staged_legs(direction="BUY", tps=[4185, 4190, 4195], near_edge=4180,
                               deep_edge=4176, sl=4168, atr=14, current_price=4179, cfg=D)
    toe = next(l for l in legs if l.tranche == "toe_in")
    assert toe.order_type == "MARKET" and float(toe.entry) == 4179   # price already in the zone


def test_build_staged_legs_sell_mirror():
    legs = S.build_staged_legs(direction="SELL", tps=[4171, 4166, 4161], near_edge=4176,
                               deep_edge=4180, sl=4188, atr=14, current_price=4174, cfg=D)
    rec = next(l for l in legs if l.tranche == "reclaim")
    assert rec.order_type == "STOP" and float(rec.entry) == 4180 - 0.10 * 14   # offset below deep for SELL


def test_build_staged_legs_point_entry_folds_runner_into_toe_in():
    # #140: point entry (near == deep == 4095) has no distinct deep edge to rest a
    # runner LIMIT against, so the runner leg must fold into the toe-in (no separate
    # LIMIT runner). Mirrors live sig752: BUY 4095, TPs [4100,4105,4110,4130].
    legs = S.build_staged_legs(direction="BUY", tps=[4100, 4105, 4110, 4130],
                               near_edge=4095, deep_edge=4095, sl=4078, atr=15.19,
                               current_price=4094.75, cfg=D)
    assert not any(l.tranche == "runner" for l in legs)          # no rejected limit-at-market
    toe = [l for l in legs if l.tranche == "toe_in"]
    assert {l.tp_index for l in toe} == {1, 4}                    # nearest + folded-in farthest
    # price already crossed the point -> toe-in (incl. the folded runner) is MARKET
    assert all(l.order_type == "MARKET" for l in toe)
    assert any(l.tranche == "reclaim" for l in legs)             # the deferred tail still exists
    assert all(l.valid for l in legs)


def test_build_staged_legs_zone_entry_still_has_runner():
    # Guard the fold is point-entry-only: a real zone (near != deep) keeps its runner.
    legs = S.build_staged_legs(direction="BUY", tps=[4100, 4105, 4110, 4130],
                               near_edge=4098, deep_edge=4095, sl=4078, atr=15.19,
                               current_price=4099, cfg=D)
    assert any(l.tranche == "runner" for l in legs)


# ---- toe-in honours the MARKET / "enter now" hint (#151) ----
def _sig784(**kw):
    """Live sig784: SELL, hint=MARKET, zone 4045-4050, SL 4060, price BELOW the
    zone (4043.4) — DemoA/B filled at market, DemoC rested a LIMIT@4045 and never
    filled. near edge for a SELL is the LOW side (4045)."""
    args = dict(direction="SELL", tps=[4035, 4030, 4025], near_edge=4045,
                deep_edge=4050, sl=4060, atr=14, current_price=4043.4, cfg=D)
    args.update(kw)
    return S.build_staged_legs(**args)


def test_toe_in_rests_a_limit_without_the_hint():
    # Unchanged behaviour: no hint -> the toe-in still waits at the near edge.
    toe = next(l for l in _sig784() if l.tranche == "toe_in")
    assert toe.order_type == "LIMIT" and float(toe.entry) == 4045


def test_toe_in_market_on_hint_when_price_is_outside_the_zone():
    # #151: with the hint, DemoC enters at the live price like DemoA/B instead of
    # resting a sell-limit above a falling market.
    toe = next(l for l in _sig784(market_hint=True) if l.tranche == "toe_in")
    assert toe.order_type == "MARKET" and float(toe.entry) == 4043.4
    assert toe.valid


def test_hint_does_not_disturb_runner_and_reclaim():
    # Only the toe-in changes; the rest stays staged relative to the zone.
    legs = _sig784(market_hint=True)
    runner = next(l for l in legs if l.tranche == "runner")
    rec = next(l for l in legs if l.tranche == "reclaim")
    assert runner.order_type == "LIMIT" and float(runner.entry) == 4050
    assert rec.order_type == "STOP" and float(rec.entry) == 4050 - 0.10 * 14


def test_hint_is_bounded_by_the_chase_tolerance():
    # 20 below a 4045 sell edge, tolerance = 0.25 x |4045-4060| = 3.75 -> too far
    # to chase; rest at the edge exactly as the baseline planner would (#67).
    toe = next(l for l in _sig784(current_price=4025, market_hint=True)
               if l.tranche == "toe_in")
    assert toe.order_type == "LIMIT" and float(toe.entry) == 4045


def test_hint_market_is_not_a_chase_when_price_is_already_better():
    # Price ABOVE a sell edge = at/better than the level: a market fill is not a
    # chase, and this already went MARKET before the hint existed.
    toe = next(l for l in _sig784(current_price=4047, market_hint=True)
               if l.tranche == "toe_in")
    assert toe.order_type == "MARKET" and float(toe.entry) == 4047


def test_hint_buy_mirror():
    legs = S.build_staged_legs(direction="BUY", tps=[4185, 4190, 4195], near_edge=4180,
                               deep_edge=4176, sl=4168, atr=14, current_price=4182,
                               cfg=D, market_hint=True)
    toe = next(l for l in legs if l.tranche == "toe_in")
    # 2 above a 4180 buy edge, tolerance 0.25 x 12 = 3 -> chase allowed
    assert toe.order_type == "MARKET" and float(toe.entry) == 4182
    far = S.build_staged_legs(direction="BUY", tps=[4185, 4190, 4195], near_edge=4180,
                              deep_edge=4176, sl=4168, atr=14, current_price=4190,
                              cfg=D, market_hint=True)
    toe_far = next(l for l in far if l.tranche == "toe_in")
    assert toe_far.order_type == "LIMIT" and float(toe_far.entry) == 4180


def test_hint_atr_tolerance_widens_the_chase():
    # chase_tolerance_atr is the other term (larger of the two wins): 0.5 x 14 = 7
    # allows a chase the R-term (3.75) alone would have rested.
    toe = next(l for l in _sig784(current_price=4040, market_hint=True,
                                  chase_tolerance_atr=0.5) if l.tranche == "toe_in")
    assert toe.order_type == "MARKET" and float(toe.entry) == 4040


# ---- entry-TTL window: deployed TTL + absolute ceiling (#158) ----
def test_deployed_ttl_defaults_to_the_entry_ttl():
    # 0/unset = inherit, i.e. the behaviour that shipped with #129.
    assert S.deployed_ttl_minutes(D, 60) == 60
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": 0}, 45) == 45
    assert S.deployed_ttl_minutes({}, 90) == 90


def test_deployed_ttl_overrides_when_set():
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": 15}, 60) == 15
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": "20"}, 60) == 20
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": "junk"}, 60) == 60   # fail-safe


def test_entry_age_ceiling_is_off_by_default():
    assert S.entry_age_exceeded(D, 10_000) is False
    assert S.entry_age_exceeded({"max_entry_age_minutes": 0}, 10_000) is False


def test_entry_age_ceiling_fires_past_the_limit():
    cfg = {"max_entry_age_minutes": 90}
    assert S.entry_age_exceeded(cfg, 89) is False
    assert S.entry_age_exceeded(cfg, 90) is False        # strictly past it
    assert S.entry_age_exceeded(cfg, 91) is True


def _expiry(cfg, leg_age, entry_age, deployed, entry_ttl=60):
    return S.entry_expiry_reason(cfg, leg_age_minutes=leg_age,
                                 entry_age_minutes=entry_age,
                                 entry_ttl_minutes=entry_ttl, deployed=deployed)


def test_expiry_default_config_reproduces_todays_behaviour():
    # A runner deployed at T+45 rests a fresh 60 -> alive at T+104, gone past its own TTL.
    assert _expiry(D, leg_age=59, entry_age=104, deployed=True) is None
    assert _expiry(D, leg_age=61, entry_age=106, deployed=True) == "leg_ttl"
    # A toe-in placed at signal time answers to the entry TTL, not the deployed one.
    assert _expiry(D, leg_age=61, entry_age=61, deployed=False) == "leg_ttl"


def test_expiry_deployed_ttl_shortens_the_resting_window():
    cfg = dict(D, deployed_ttl_minutes=15)
    assert _expiry(cfg, leg_age=16, entry_age=61, deployed=True) == "leg_ttl"
    # ...and leaves a signal-time leg alone: it is not a deployed one.
    assert _expiry(cfg, leg_age=16, entry_age=16, deployed=False) is None


def test_expiry_ceiling_wins_over_a_leg_ttl_that_has_not_elapsed():
    # The whole point: a late deploy reset the leg clock, so only the absolute
    # ceiling can bound the entry.
    cfg = dict(D, max_entry_age_minutes=90)
    assert _expiry(cfg, leg_age=5, entry_age=120, deployed=True) == "max_entry_age"
    assert _expiry(cfg, leg_age=5, entry_age=60, deployed=True) is None


def test_expiry_ceiling_applies_to_the_toe_in_too():
    cfg = dict(D, max_entry_age_minutes=30)
    assert _expiry(cfg, leg_age=10, entry_age=31, deployed=False) == "max_entry_age"


def test_expiry_reports_the_ceiling_when_both_would_fire():
    cfg = dict(D, max_entry_age_minutes=90, deployed_ttl_minutes=15)
    assert _expiry(cfg, leg_age=999, entry_age=999, deployed=True) == "max_entry_age"


def test_clean_staged_config_accepts_the_new_ttl_keys():
    out = S.clean_staged_config({"deployed_ttl_minutes": "15",
                                 "max_entry_age_minutes": 90})
    assert out == {"deployed_ttl_minutes": 15, "max_entry_age_minutes": 90}
    for bad in ({"deployed_ttl_minutes": -1}, {"max_entry_age_minutes": "abc"}):
        try:
            S.clean_staged_config(bad)
            assert False, f"expected reject for {bad}"
        except ValueError:
            pass


# ---- config validation (#129 Phase 1) ----
def test_clean_entry_style():
    assert S.clean_entry_style("STAGED") == "staged"
    assert S.clean_entry_style("limit") == "limit"
    for bad in ("staggered", "", None):
        try:
            S.clean_entry_style(bad)
            assert False, f"expected reject for {bad!r}"
        except ValueError:
            pass


def test_clean_staged_config_valid_and_coerced():
    out = S.clean_staged_config({"enabled": True, "deployed_ttl_minutes": "30"})
    assert out == {"enabled": True, "deployed_ttl_minutes": 30}
    assert S.clean_staged_config({"nonsense_key": 5}) is None      # unknown keys dropped
    assert S.clean_staged_config(None) is None


# The 13 that #250 deleted. Named individually so this fails loudly if one is
# ever quietly reintroduced as config instead of as a constant.
_DELETED_KNOBS = ("toe_in_tps", "runner_tps", "max_deferred_fraction",
                  "min_deferred_fraction", "reclaim_break_atr", "reclaim_break_cash",
                  "reclaim_break_max_frac_of_stop", "reclaim_break_abs_cap",
                  "stop_offset_atr", "runner_ttl_minutes",
                  "reclaim_pending_ttl_minutes", "reclaim_armed_ttl_minutes",
                  "min_stop_atr")


@pytest.mark.parametrize("knob", _DELETED_KNOBS)
def test_a_deleted_tuning_knob_is_dropped_never_stored(knob):
    """DROPPED, not rejected (#250). A saved row written by an older client still
    carries these; 422-ing them would make the whole strategy unsaveable, and the
    value would be ignored at read time anyway."""
    assert S.clean_staged_config({knob: 0.5}) is None
    assert S.clean_staged_config({"enabled": True, knob: 0.5}) == {"enabled": True}


@pytest.mark.parametrize("knob", _DELETED_KNOBS)
def test_a_deleted_knob_is_absent_from_the_effective_config(knob):
    assert knob not in S.staged_config({knob: 0.5})
    assert knob not in S.DEFAULT_STAGED


def test_the_frozen_geometry_still_holds_the_values_it_shipped_with():
    """#250 removed the CHOICE, not the behaviour. If a constant here ever drifts
    from the default it replaced, staged entry silently changes shape — so the
    numbers are pinned, and changing one has to be deliberate."""
    assert (S.TOE_IN_TPS, S.RUNNER_TPS) == (1, 1)
    assert (S.MIN_DEFERRED_FRACTION, S.MAX_DEFERRED_FRACTION) == (0.20, 0.60)
    assert S.RECLAIM_BREAK_ATR == 0.35 and S.RECLAIM_BREAK_CASH == 0.0
    assert S.RECLAIM_BREAK_MAX_FRAC_OF_STOP == 0.5
    assert S.RECLAIM_BREAK_ABS_CAP == 8.0
    assert S.STOP_OFFSET_ATR == 0.10
    assert S.RUNNER_TTL_MINUTES == 45
    assert S.RECLAIM_PENDING_TTL_MINUTES == 60
    assert S.RECLAIM_ARMED_TTL_MINUTES == 60
    assert S.MIN_STOP_ATR == 0.5


def test_clean_staged_config_rejects_bad_values():
    for bad in ({"enabled": "yes"},                    # bool required
                {"deployed_ttl_minutes": -1},          # negative
                {"max_entry_age_minutes": "abc"}):     # non-numeric
        try:
            S.clean_staged_config(bad)
            assert False, f"expected reject for {bad}"
        except ValueError:
            pass
    try:
        S.clean_staged_config([1, 2])                  # not an object
        assert False
    except ValueError:
        pass


def test_the_158_safety_bounds_stay_configurable():
    """Not tuning numbers — brakes. Off by default, and #250's list of 13 leaves
    them out on purpose."""
    out = S.clean_staged_config({"deployed_ttl_minutes": 30, "max_entry_age_minutes": 90})
    assert out == {"deployed_ttl_minutes": 30, "max_entry_age_minutes": 90}


def test_staged_config_overlay_completes_cfg():
    cfg = S.staged_config({"deployed_ttl_minutes": 30})
    assert cfg["deployed_ttl_minutes"] == 30           # override applied
    assert cfg["enabled"] == D["enabled"]              # default filled
    assert S.staged_config(None) == D                 # no stored -> pure defaults


def test_broken_modifier_never_crashes_or_opens():
    boom = lambda **k: (_ for _ in ()).throw(RuntimeError("bad decider"))
    # on a WAIT it stays WAIT; on a DEPLOY the base survives (conservative)
    assert S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4179), cfg=D,
                            deciders=[boom]).action == S.WAIT
    assert S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4176), cfg=D,
                            deciders=[boom]).action == S.DEPLOY
