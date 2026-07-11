"""Shadow analytics sidecar foundation (#52): the isolation harness + window
summary. Pure (heavy DB imports are deferred), so it runs on a bare box."""
import asyncio

from beacon_core.analysis.sidecar import (AnalyticsCtx, run_estimators,
                                          build_window, DEFAULT_ANALYTICS)


def _ctx(**kw):
    base = dict(signal_id=1, symbol="XAUUSD", direction="BUY", price=100.0,
                timeframe="1h", closes=[float(i) for i in range(1, 11)])
    base.update(kw)
    return AnalyticsCtx(**base)


def test_default_analytics_on_and_shadow():
    # pure observability: enabled by default (off the hot path), with a primary tf
    assert DEFAULT_ANALYTICS["enabled"] is True
    assert DEFAULT_ANALYTICS["timeframe"] == "1h"


def test_harness_isolates_failures():
    def good(c): return {"n": len(c.closes)}
    def boom(c): raise ValueError("kaboom")
    def skip(c): return None                       # None -> not recorded, not degraded
    async def aknn(c): return {"k": 3}             # async estimators supported
    a, d = asyncio.run(run_estimators(_ctx(), {
        "good": good, "boom": boom, "skip": skip, "knn": aknn}))
    assert a == {"good": {"n": 10}, "knn": {"k": 3}}
    assert d == ["boom"]                            # only the raiser is degraded


def test_harness_never_raises_even_if_all_fail():
    def boom(c): raise RuntimeError("x")
    a, d = asyncio.run(run_estimators(_ctx(), {"a": boom, "b": boom}))
    assert a == {} and sorted(d) == ["a", "b"]      # swallowed, never propagates


def test_build_window_is_compact_and_reproducible():
    w = build_window(_ctx(), max_bars=5)
    assert w["timeframe"] == "1h" and w["n"] == 5
    assert w["closes"] == [6.0, 7.0, 8.0, 9.0, 10.0]   # last N, rounded
    assert w["price"] == 100.0


def test_build_window_handles_empty_and_no_price():
    w = build_window(_ctx(closes=[], price=None), max_bars=200)
    assert w["n"] == 0 and w["closes"] == [] and w["price"] is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
