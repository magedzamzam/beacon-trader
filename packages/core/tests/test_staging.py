"""Confirmation-staged entry engine (#129) — pure, no DB/broker/clock.

Covers the partition (by TP tier), the break-distance clamps, and the DECIDE
engine over the four synthetic price paths the acceptance criteria name:
straight-to-SL, pullback-continue, V-bounce, fakeout-reclaim — plus the
conservative-only invariant of the modifier-decider pipeline."""
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
    # no ATR -> cash fallback (0 default -> None, fail-safe: never arms)
    assert S.break_distance(D, atr=None, sl_dist=8.0) is None
    assert S.break_distance({**D, "reclaim_break_cash": 3.0}, atr=None, sl_dist=100.0) == 3.0


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
    d = _dec(S.RUNNER, S.PENDING, _ctx(4176))                            # at deep
    assert d.action == S.DEPLOY and d.mode == S.MODE_LIMIT and d.level == DEEP


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
    assert S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=ctx, cfg=D).action == S.DEPLOY
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
    out = S.clean_staged_config({"toe_in_tps": "2", "reclaim_break_atr": "0.4",
                                 "enabled": True, "runner_ttl_minutes": 30})
    assert out == {"toe_in_tps": 2, "reclaim_break_atr": 0.4, "enabled": True,
                   "runner_ttl_minutes": 30}
    assert S.clean_staged_config({"nonsense_key": 5}) is None      # unknown keys dropped
    assert S.clean_staged_config(None) is None


def test_clean_staged_config_rejects_bad_values():
    for bad in ({"max_deferred_fraction": 1.5},        # frac out of range
                {"reclaim_break_atr": -1},             # negative
                {"toe_in_tps": "abc"},                 # non-numeric
                {"enabled": "yes"},                    # bool required
                {"min_deferred_fraction": 0.8, "max_deferred_fraction": 0.5}):  # min>max
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


def test_staged_config_overlay_completes_cfg():
    cfg = S.staged_config({"reclaim_break_atr": 0.5})
    assert cfg["reclaim_break_atr"] == 0.5             # override applied
    assert cfg["runner_ttl_minutes"] == D["runner_ttl_minutes"]   # default filled
    assert S.staged_config(None) == D                 # no stored -> pure defaults


def test_broken_modifier_never_crashes_or_opens():
    boom = lambda **k: (_ for _ in ()).throw(RuntimeError("bad decider"))
    # on a WAIT it stays WAIT; on a DEPLOY the base survives (conservative)
    assert S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4179), cfg=D,
                            deciders=[boom]).action == S.WAIT
    assert S.decide_tranche(role=S.RUNNER, state=S.PENDING, ctx=_ctx(4176), cfg=D,
                            deciders=[boom]).action == S.DEPLOY
