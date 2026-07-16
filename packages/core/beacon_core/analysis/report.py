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
from ._util import dig_num, adverse_side
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




def _summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4)}


async def channel_regime_report(session, frm=None, to=None) -> dict:
    """Per-channel × regime performance + regime mix by channel + a
    win/loss feature read, all off the labelled analytics→trade join.
    Optional [frm, to) window anchored on the SIGNAL time (Signal.created_at) —
    the time the report groups by (#58)."""
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Source, Trade

    q = (select(Source.name, SignalAnalytics.regime, SignalAnalytics.analytics,
                Trade.realized_pl)
         .join(Signal, Signal.id == SignalAnalytics.signal_id)
         .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

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
            v = dig_num(analytics or {}, *path)
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


def _structure_membership(features: dict) -> tuple:
    """(in_fvg, in_ob): is the entry price inside an UNFILLED FVG / UNMITIGATED
    OB on any captured timeframe? The structure keys embed their params
    (e.g. "fvg_0.25_50"), so match by prefix (#59)."""
    in_fvg = in_ob = False
    for _tf, block in (features or {}).items():
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if not isinstance(val, dict):
                continue
            inside = bool(val.get("present")) and val.get("dist_pct") == 0
            if key.startswith("fvg"):
                in_fvg = in_fvg or inside
            elif key.startswith("order_block"):
                in_ob = in_ob or inside
    return in_fvg, in_ob


async def structure_outcome_report(session, frm=None, to=None) -> dict:
    """The FVG/OB-vs-outcome cut (#59): win-rate & expectancy when a signal's
    entry sits inside an unfilled Fair Value Gap / unmitigated Order Block vs not,
    overall and per channel/regime, with Beta-Binomial credible intervals. Joins
    signal_features (structure) + signal_analytics (regime) -> trades.realized_pl.
    Shadow only — nothing gates on it."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalFeature, SignalAnalytics, Signal, Source, Trade

    q = (select(Source.name, SignalFeature.features, SignalAnalytics.regime,
                Trade.realized_pl)
         .join(Signal, Signal.id == SignalFeature.signal_id)
         .join(Trade, Trade.signal_id == SignalFeature.signal_id)
         .outerjoin(SignalAnalytics, SignalAnalytics.signal_id == SignalFeature.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    agg = {"fvg": defaultdict(lambda: defaultdict(_cell)),
           "ob": defaultdict(lambda: defaultdict(_cell))}
    overall_n = overall_wins = 0

    for name, feats, regime, pl in rows:
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        overall_n += 1
        overall_wins += 1 if win else 0
        in_fvg, in_ob = _structure_membership(feats)
        chan = name or "Unattributed"
        reg = regime or "unknown"
        for struct, inside in (("fvg", in_fvg), ("ob", in_ob)):
            mem = "inside" if inside else "outside"
            for scope, label in (("overall", "all"), ("channel", chan), ("regime", reg)):
                b = agg[struct][(scope, label)][mem]
                b["n"] += 1
                b["wins"] += 1 if win else 0
                b["pl"] += pl

    base = (overall_wins / overall_n) if overall_n else 0.5

    def _rows(struct):
        out = []
        for (scope, label), mems in agg[struct].items():
            for mem, b in mems.items():
                if not b["n"]:
                    continue
                post = posterior(b["wins"], b["n"], base)
                out.append({"scope": scope, "label": label, "membership": mem,
                            "n": b["n"], "win_rate": round(b["wins"] / b["n"], 4),
                            "expectancy": round(b["pl"] / b["n"], 4),
                            "ci_low": round(post["ci_low"], 4),
                            "ci_high": round(post["ci_high"], 4)})
        out.sort(key=lambda r: (r["scope"] != "overall", r["label"], r["membership"]))
        return out

    return {
        "n_labelled": overall_n, "base_rate": round(base, 4),
        "fvg": _rows("fvg"), "ob": _rows("ob"),
        "note": ("Structure (FVG/OB) vs outcome — SHADOW only, measure-before-gate "
                 "(#59). 'inside' = entry price within an unfilled FVG / unmitigated "
                 "OB on any captured timeframe. Small-n: trust the credible interval."),
    }


async def execution_tax_report(session, frm=None, to=None) -> dict:
    """Per-channel win-rate under BOTH labels (#63), computed on the signals that
    carry both: the channel's SIGNAL-QUALITY outcome (its own claims — TP1+ vs SL,
    independent of our fills/stops) and our BOT-REALIZED outcome (realized_pl>0).
    The GAP = signal_quality_wr − bot_realized_wr is the *execution tax*: setups
    that worked but we didn't capture. Beta-Binomial credible intervals. Shadow /
    read-only — nothing gates on this; it sizes the execution-fix backlog."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import Signal, Source, Trade, SignalClaim
    from .bayes import signal_quality_label

    tq = (select(Trade.signal_id, Trade.realized_pl, Source.name)
          .join(Signal, Signal.id == Trade.signal_id)
          .outerjoin(Source, Source.id == Signal.source_id)
          .where(Trade.status == "closed"))
    if frm is not None:
        tq = tq.where(Signal.created_at >= frm)
    if to is not None:
        tq = tq.where(Signal.created_at < to)
    trows = (await session.execute(tq)).all()

    sids = [sid for sid, pl, _ in trows if pl is not None and sid is not None]
    claims_by = defaultdict(list)
    if sids:
        for c in (await session.execute(
                select(SignalClaim).where(SignalClaim.signal_id.in_(sids)))).scalars().all():
            claims_by[c.signal_id].append(c)

    def _cell():
        return {"n": 0, "sq_wins": 0, "br_wins": 0}

    by_chan = defaultdict(_cell)
    overall = _cell()
    for sid, pl, name in trows:
        if pl is None:
            continue
        sq = signal_quality_label(claims_by.get(sid))
        if sq is None:                       # no clean channel outcome -> excluded
            continue
        br = float(pl) > 0
        for cell in (by_chan[name or "Unattributed"], overall):
            cell["n"] += 1
            cell["sq_wins"] += 1 if sq else 0
            cell["br_wins"] += 1 if br else 0

    on = overall["n"]
    base_sq = (overall["sq_wins"] / on) if on else 0.5
    base_br = (overall["br_wins"] / on) if on else 0.5

    def _fmt(cell):
        n = cell["n"]
        if not n:
            return None
        sq, br = posterior(cell["sq_wins"], n, base_sq), posterior(cell["br_wins"], n, base_br)
        return {"n": n,
                "signal_quality_wr": round(cell["sq_wins"] / n, 4),
                "sq_ci": [round(sq["ci_low"], 4), round(sq["ci_high"], 4)],
                "bot_realized_wr": round(cell["br_wins"] / n, 4),
                "br_ci": [round(br["ci_low"], 4), round(br["ci_high"], 4)],
                "execution_tax": round((cell["sq_wins"] - cell["br_wins"]) / n, 4)}

    by_channel = [{"channel": c, **_fmt(cell)} for c, cell in by_chan.items() if cell["n"]]
    by_channel.sort(key=lambda r: -r["execution_tax"])       # biggest tax first

    return {
        "n_labelled": on, "overall": _fmt(overall), "by_channel": by_channel,
        "note": ("Execution tax (#63) — signal-quality WR (channel claims: TP1+ "
                 "vs SL) minus bot-realized WR (realized_pl>0), on signals with "
                 "both labels. A positive gap = the setup worked but our execution "
                 "didn't capture it (fills #25 / TTL #40 / stops). Shadow only. "
                 "Ambiguous/contradictory claims are excluded, not counted as loss."),
    }


async def trend_alignment_outcome_report(session, frm=None, to=None, *,
                                         timeframe="4h", ema_period=200) -> dict:
    """The aligned-vs-counter split as a first-class metric (#72): win-rate,
    net PnL and expectancy for trend-ALIGNED vs COUNTER-trend entries, overall
    and per channel, with Beta-Binomial credible intervals. Classifies each
    labelled trade from its persisted `signal_features` (price vs `timeframe`
    EMA`ema_period` at signal time) — the exact definition the live filter (#48)
    gates on — joined to trades.realized_pl. Unknown-trend signals are excluded
    (the filter fails open on them). Read-only; nothing gates on this."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalFeature, Signal, Source, Trade
    from ..execution.trend_filter import alignment_from_features

    q = (select(Source.name, Signal.direction, SignalFeature.features, Trade.realized_pl)
         .join(Signal, Signal.id == SignalFeature.signal_id)
         .join(Trade, Trade.signal_id == SignalFeature.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    overall = defaultdict(_cell)                       # "aligned" | "counter"
    by_channel = defaultdict(lambda: defaultdict(_cell))
    n_class = wins_class = n_unknown = 0

    for name, direction, feats, pl in rows:
        if pl is None:
            continue
        aligned = alignment_from_features(feats, direction, timeframe, ema_period)
        if aligned is None:                            # trend unknown -> fail-open, excluded
            n_unknown += 1
            continue
        pl = float(pl)
        win = pl > 0
        key = "aligned" if aligned else "counter"
        for b in (overall[key], by_channel[name or "Unattributed"][key]):
            b["n"] += 1
            b["wins"] += 1 if win else 0
            b["pl"] += pl
        n_class += 1
        wins_class += 1 if win else 0

    base = (wins_class / n_class) if n_class else 0.5

    def _fmt(b):
        post = posterior(b["wins"], b["n"], base)
        return {"n": b["n"], "win_rate": round(b["wins"] / b["n"], 4),
                "net": round(b["pl"], 2), "expectancy": round(b["pl"] / b["n"], 4),
                "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4)}

    return {
        "timeframe": timeframe, "ema_period": ema_period,
        "n_labelled": n_class, "n_unknown_trend": n_unknown, "base_rate": round(base, 4),
        "overall": {k: _fmt(v) for k, v in overall.items() if v["n"]},
        "by_channel": {ch: {k: _fmt(v) for k, v in m.items() if v["n"]}
                       for ch, m in by_channel.items()},
        "note": ("Trend-alignment (price vs %s EMA%d at signal time) vs outcome — "
                 "SHADOW metric (#72). 'counter' = entry fighting the higher-TF "
                 "trend; #48 filter skips/de-sizes these. Unknown-trend signals "
                 "excluded (filter fails open). Small-n: trust the credible interval."
                 % (timeframe, ema_period)),
    }


def _zone_proximity_band(sm: dict, direction: str):
    """(proximity_band, adverse) from a structure_magnet block: how close is the
    nearest magnet zone, and is it on the ADVERSE side (BUY into a zone above /
    SELL into one below)?"""
    nz = (sm or {}).get("nearest_zone") or {}
    if nz.get("inside"):
        band = "inside"
    else:
        d = nz.get("dist_atr")
        band = ("near" if d is not None and d <= 0.5 else
                "mid" if d is not None and d <= 2.0 else "far")
    side = nz.get("side")
    adverse = adverse_side(direction, side)
    return band, bool(adverse)


async def structure_magnet_outcome_report(session, frm=None, to=None) -> dict:
    """Phase-2 payoff (#61): does magnet proximity / HTF-structure alignment
    predict outcome? Cuts win-rate & expectancy by `htf_alignment`, by nearest-
    zone proximity band, and by adverse-side, with Beta-Binomial credible
    intervals — off the signal_analytics(structure_magnet) -> trades join. This is
    the measurement Phase-3 filtering waits on (measure-before-gate). Shadow only."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Trade

    q = (select(SignalAnalytics.analytics, SignalAnalytics.direction, Trade.realized_pl)
         .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
         .join(Signal, Signal.id == SignalAnalytics.signal_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    cuts = {"htf_alignment": defaultdict(_cell), "proximity": defaultdict(_cell),
            "adverse_side": defaultdict(_cell)}
    overall_n = overall_wins = 0

    for analytics, direction, pl in rows:
        sm = (analytics or {}).get("structure_magnet")
        if pl is None or not sm:
            continue
        pl = float(pl)
        win = pl > 0
        overall_n += 1
        overall_wins += 1 if win else 0
        band, adverse = _zone_proximity_band(sm, direction)
        for dim, key in (("htf_alignment", sm.get("htf_alignment") or "unknown"),
                         ("proximity", band),
                         ("adverse_side", "adverse" if adverse else "clear")):
            b = cuts[dim][key]
            b["n"] += 1
            b["wins"] += 1 if win else 0
            b["pl"] += pl

    base = (overall_wins / overall_n) if overall_n else 0.5

    def _rows(dim):
        out = []
        for key, b in cuts[dim].items():
            if not b["n"]:
                continue
            post = posterior(b["wins"], b["n"], base)
            out.append({"bucket": key, "n": b["n"],
                        "win_rate": round(b["wins"] / b["n"], 4),
                        "expectancy": round(b["pl"] / b["n"], 4),
                        "ci_low": round(post["ci_low"], 4),
                        "ci_high": round(post["ci_high"], 4)})
        out.sort(key=lambda r: (-r["ci_low"], -r["n"]))
        return out

    return {
        "n_labelled": overall_n, "base_rate": round(base, 4),
        "htf_alignment": _rows("htf_alignment"),
        "proximity": _rows("proximity"),
        "adverse_side": _rows("adverse_side"),
        "note": ("Magnet/structure vs outcome — SHADOW only (#61). Phase-3 "
                 "filtering waits on this: enable structure.filter once a bucket's "
                 "credible interval separates from the base rate at N>=30."),
    }
