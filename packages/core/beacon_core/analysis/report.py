"""Signal ↔ channel ↔ regime correlation report (#53) — the payoff that turns
the shadow sidecar into decisions. Answers "which channel works in which
regime" from the labelled join signal_analytics → signals → trades.realized_pl,
with Beta-Binomial credible intervals (reuses analysis.bayes) so small-n buckets
are shrunk toward the base rate instead of over-trusted.

Read-only / observability. Epoch-awareness caveat (per #51): stats are pooled
across the whole history — a config change creates a regime break the caller
should weigh before acting.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .bayes import posterior
from ..logging import get_logger

log = get_logger("analytics.report")

# numeric estimator fields to summarise for a feature→outcome read
_FEATURE_PATHS = {
    "hurst": ("hurst", "value"),
    "adx": ("regime", "adx"),
    "atr_pct": ("regime", "atr_pct"),
    "realized_vol": ("regime", "realized_vol"),
    "kalman_slope": ("kalman", "slope"),
    "vwap_z": ("vwap_deviation", "z"),
}


def _dig(d, path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur if isinstance(cur, (int, float)) else None


def _summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4)}


async def channel_regime_report(session) -> dict:
    """Per-channel × regime performance + regime mix by channel + a
    win/loss feature read, all off the labelled analytics→trade join."""
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Source, Trade

    rows = (await session.execute(
        select(Source.name, SignalAnalytics.regime, SignalAnalytics.analytics,
               Trade.realized_pl)
        .join(Signal, Signal.id == SignalAnalytics.signal_id)
        .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
        .outerjoin(Source, Source.id == Signal.source_id))).all()

    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
    chan_regime = defaultdict(lambda: defaultdict(int))
    feat_win = defaultdict(list)
    feat_loss = defaultdict(list)
    overall_n = overall_wins = 0

    for name, regime, analytics, pl in rows:
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        chan = name or "Unattributed"
        reg = regime or "unknown"
        b = buckets[(chan, reg)]
        b["n"] += 1
        b["wins"] += 1 if win else 0
        b["pl"] += pl
        chan_regime[chan][reg] += 1
        overall_n += 1
        overall_wins += 1 if win else 0
        for fname, path in _FEATURE_PATHS.items():
            v = _dig(analytics or {}, path)
            if v is not None:
                (feat_win if win else feat_loss)[fname].append(v)

    base = (overall_wins / overall_n) if overall_n else 0.5

    by_cr = []
    for (chan, reg), b in buckets.items():
        post = posterior(b["wins"], b["n"], base)
        by_cr.append({
            "channel": chan, "regime": reg, "n": b["n"],
            "win_rate": round(b["wins"] / b["n"], 4),
            "expectancy": round(b["pl"] / b["n"], 4),
            "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4),
        })
    # most reliably-good first (highest lower credible bound), then by size
    by_cr.sort(key=lambda r: (-r["ci_low"], -r["n"]))

    features = {f: {"win": _summary(feat_win.get(f, [])),
                    "loss": _summary(feat_loss.get(f, []))}
                for f in _FEATURE_PATHS}

    return {
        "base_rate": round(base, 4),
        "n_labelled": overall_n,
        "by_channel_regime": by_cr,
        "regime_mix_by_channel": {c: dict(m) for c, m in chan_regime.items()},
        "feature_by_outcome": features,
        "note": ("Shadow analytics — observability only, nothing gates on this. "
                 "Stats pooled across history; weigh config-change regime breaks."),
    }
