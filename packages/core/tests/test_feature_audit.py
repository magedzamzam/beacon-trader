"""#168 — what the discriminative-value audit changed, and the switch it added.

The audit's job was to find features that cannot inform a decision and stop
recording them. Two of the three headline suspects turned out to be dead BY BUG
rather than dead by nature, so they are repaired here instead of disabled — a
broken estimator that is switched off is still broken, and its history is still
garbage. The third (k-NN) turned out not to be dead at all.

Also adds the reversible off-switch the issue asked for, so a feature that IS
genuinely shown dead can be stopped from config without deleting code or history.
"""
import asyncio
import math

from beacon_core.analysis import estimators as E
from beacon_core.analysis import sidecar
from beacon_core.analysis.report import _structure_membership, STRUCTURE_NEAR_PCT


def _ramp(n=64, step=0.5, start=100.0):
    return [start + i * step for i in range(n)]


def _walk(n=220, start=2000.0, seed=7):
    """A deterministic pseudo-random price path — a stand-in for the real 1h gold
    window, which is what exposed the bug."""
    out, x, s = [], start, seed
    for _ in range(n):
        s = (1103515245 * s + 12345) % 2147483648
        x *= 1.0 + ((s / 2147483648) - 0.5) * 0.004
        out.append(x)
    return out


# ---- hurst: R/S measured the level, not the increments ----------------------
def test_hurst_stays_inside_the_valid_range_on_a_price_path():
    """The captured evidence: 264/264 signals had H in [0.9627, 1.0591], median
    1.0065 — a QUARTER of them above 1.0, which no Hurst exponent can be. R/S
    over a price series returns ~1 for any instrument because prices are
    near-integrated; it has to run on the returns."""
    h = E.hurst_rs(_walk())
    assert h is not None
    assert 0.0 <= h <= 1.0
    assert abs(h - 1.0) > 0.15          # not pinned at the saturated value


def test_hurst_on_the_level_reproduces_the_bug():
    """Kept as the counter-example: this is what the old call did, and why the
    feature had no usable spread."""
    assert E.hurst_rs(_walk(), on_returns=False) > 0.9


def test_hurst_still_separates_persistent_from_anti_persistent():
    assert E.hurst_rs(_ramp()) > 0.6
    assert E.hurst_rs([100 + (2 if i % 2 else -2) for i in range(64)]) < 0.5
    assert E.hurst_rs([1, 2, 3]) is None


def test_hurst_survives_a_flat_or_non_positive_series():
    """Differencing introduces a log and a division; neither may raise on the
    degenerate inputs a thin or broken bar feed produces."""
    assert E.hurst_rs([100.0] * 60) in (None, 0.0) or math.isfinite(E.hurst_rs([100.0] * 60))
    assert E.hurst_rs([0.0] * 60) is None
    assert E.hurst_rs([-1.0] * 60) is None


# ---- regime: the constant label had TWO causes, not one ---------------------
def test_saturated_hurst_no_longer_forces_trending():
    """`classify_regime` ORed `hurst > 0.55` with the ADX test, so a saturated H
    labelled EVERY signal 'trending' on its own. #111's null ADX read was blamed
    for the 264/264 constant; repairing that read alone would not have moved it
    while this sat upstream.

    #168 fixed the ESTIMATOR (R/S on log returns) and the label still went
    degenerate — 117/117 in the week to 2026-08-22 — because the repaired H
    lands in [0.5063, 1.0591] with a 5th percentile of 0.5579, which is still
    above 0.55 on 644 of 671 rows. A term that is true 96% of the time is not a
    term. #255 took Hurst out of the vote entirely: no value of H, saturated or
    not, can decide the label now."""
    for h in (0.99, 0.52, 1.06, 0.5579):
        assert E.classify_regime(None, None, None, h) == "unknown"   # ADX decides, or nobody does
        assert E.classify_regime(10, 0.2, 0.1, h) == "ranging"       # low ADX, any H
        assert E.classify_regime(30, 0.2, 0.1, h) == "trending"      # high ADX, any H
    # a real path must not land on one side by construction any more
    labels = {E.classify_regime(adx, None, None, E.hurst_rs(_walk(seed=s)))
              for s, adx in ((3, 10.0), (7, 30.0), (11, 24.9), (23, 25.0), (41, 40.0))}
    assert labels == {"trending", "ranging"}


def test_regime_still_trends_on_a_real_adx():
    """The repair must not cost the signal that DOES work: ADX >= 25 still says
    trending regardless of what Hurst thinks."""
    feats = {"1h": {"adx_14": {"adx": 34.0}, "atr_14": {"pct": 0.2}}}

    class _Ctx:
        closes, features, price, timeframe = _walk(), feats, 2000.0, "1h"
        session = sl = entry_from = entry_to = None
        tps = []

    assert E.regime(_Ctx())["label"] == "trending"


# ---- the reversible off switch ---------------------------------------------
class _CfgCtx:
    def __init__(self, config):
        self.config = config
        self.signal_id = 1


def _run(ctx, estimators):
    return asyncio.run(sidecar.run_estimators(ctx, estimators))


def test_disabled_estimators_parses_both_shapes():
    assert sidecar.disabled_estimators({"disabled": ["knn", " hurst "]}) == {"knn", "hurst"}
    assert sidecar.disabled_estimators({"disabled": "knn, hurst"}) == {"knn", "hurst"}
    assert sidecar.disabled_estimators({"disabled": []}) == set()
    assert sidecar.disabled_estimators({}) == set()
    assert sidecar.disabled_estimators(None) == set()


def test_a_disabled_estimator_does_not_run_and_is_not_degraded():
    """Skipped is not degraded. `degraded` means 'tried and failed' and is the
    alarm that an estimator broke; folding deliberate switch-offs into it would
    make the one signal that matters unreadable."""
    calls = []

    def _a(ctx):
        calls.append("a")
        return {"value": 1}

    def _b(ctx):
        calls.append("b")
        return {"value": 2}

    analytics, degraded = _run(_CfgCtx({"disabled": ["b"]}), {"a": _a, "b": _b})
    assert calls == ["a"]
    assert set(analytics) - {"_contributions"} == {"a"}
    assert degraded == []


def test_disabled_survives_the_settings_overlay():
    """`overlay_config` only copies keys that exist in the defaults, so the switch
    is inert unless it is declared there — which is exactly the failure that would
    look like 'the SQL ran but nothing changed'."""
    from beacon_core.confutil import overlay_config

    cfg = overlay_config(sidecar.DEFAULT_ANALYTICS, {"disabled": ["knn"]})
    assert cfg["disabled"] == ["knn"]
    assert sidecar.DEFAULT_ANALYTICS["disabled"] == []      # defaults not mutated


def test_nothing_is_disabled_by_default():
    """An estimator has to be SHOWN dead. The audit found the suspects were dead
    by bug, so the shipped default disables nothing."""
    assert sidecar.DEFAULT_ANALYTICS["disabled"] == []
    analytics, _ = _run(_CfgCtx({}), {"a": lambda c: {"value": 1}})
    assert "a" in analytics


def test_disabling_an_unknown_name_is_harmless():
    analytics, degraded = _run(_CfgCtx({"disabled": ["not_an_estimator"]}),
                               {"a": lambda c: {"value": 1}})
    assert "a" in analytics and degraded == []


# ---- FVG / OB membership: a bucket that cannot populate ---------------------
def _feats(dist_pct, present=True, key="fvg_0.4_50"):
    return {"15m": {key: {"present": present, "dist_pct": dist_pct}}}


def test_membership_counts_at_the_zone_not_only_dead_centre():
    """Strict inside-only landed on 28/386 signals for FVG and 5/386 for Order
    Block — a bucket needing a year to reach N>=30. A zone is a band, and 0.05%
    of gold is ~$2, inside the spread-and-slippage an entry actually lands in."""
    assert _structure_membership(_feats(0.0))[0] is True
    assert _structure_membership(_feats(0.02))[0] is True
    assert _structure_membership(_feats(0.5))[0] is False
    assert STRUCTURE_NEAR_PCT > 0


def test_membership_still_requires_present():
    """`present` is load-bearing: the indicator reports the nearest FILLED gap
    when no unfilled one exists, so dropping it would count mitigated zones as
    live ones."""
    assert _structure_membership(_feats(0.0, present=False))[0] is False
    assert _structure_membership(_feats(0.01, present=False))[0] is False


def test_membership_can_be_restored_to_strict_inside():
    assert _structure_membership(_feats(0.02), near_pct=0.0)[0] is False
    assert _structure_membership(_feats(0.0), near_pct=0.0)[0] is True


def test_membership_separates_fvg_from_order_block():
    ob = _feats(0.0, key="order_block_1.25_50")
    assert _structure_membership(ob) == (False, True)
    assert _structure_membership(_feats(0.0)) == (True, False)


def test_membership_ignores_malformed_blocks():
    """Never raises on a feature blob: these run over captured history that spans
    several schema epochs."""
    for bad in ({}, None, {"15m": None}, {"15m": {"fvg_0.4_50": None}},
                {"15m": {"fvg_0.4_50": {"present": True, "dist_pct": None}}},
                {"15m": {"fvg_0.4_50": {"present": True, "dist_pct": "0"}}},
                {"15m": {"fvg_0.4_50": {"present": True, "dist_pct": True}}}):
        assert _structure_membership(bad) == (False, False)
