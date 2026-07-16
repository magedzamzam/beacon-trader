from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis import bayes as B
from beacon_core.analysis import feature_vector as FV
from beacon_core.analysis.report import execution_tax_report
from beacon_core.execution import bayes_gate as BG
from beacon_core.db.models import (SignalFeature, SignalAnalytics, AiAssessment,
                                   Trade, SignalClaim)
from beacon_core.settings_store import get_setting, set_setting
from beacon_core.timeutil import parse_iso_utc
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analysis", tags=["analysis"],
                   dependencies=[Depends(require_token)])


async def _model(db: AsyncSession, min_n: int, frm=None, to=None, *,
                 label: str = B.LABEL_BOT_REALIZED, account_id: int | None = None):
    """Build the Bayesian model over the UNIFIED per-signal feature vector (#62)
    under a selectable label (#63):
      - bot_realized  (default): trade.realized_pl > 0 — did WE make money?
      - signal_quality: the channel's own claims (TP1+ vs SL) — was the SETUP good,
        independent of our execution? Ambiguous/no-claim signals are excluded.
    Optional [frm, to) window (#58) on Trade.created_at. Point-in-time: reads
    persisted per-signal rows, never a fresh recompute."""
    q = select(Trade).where(Trade.status == "closed")
    if account_id is not None:                  # #83: per-account A/B slice
        q = q.where(Trade.account_id == account_id)
    if frm is not None:
        q = q.where(Trade.created_at >= frm)
    if to is not None:
        q = q.where(Trade.created_at < to)
    trades = (await db.execute(q)).scalars().all()

    # Bulk-load every layer once, keyed by signal_id (avoids N queries).
    sf_by = {s.signal_id: s for s in (await db.execute(select(SignalFeature))).scalars().all()}
    sa_by = {s.signal_id: s for s in (await db.execute(select(SignalAnalytics))).scalars().all()}
    ai_sig, ai_exec = {}, {}
    for r in (await db.execute(select(AiAssessment).where(
            AiAssessment.signal_id.isnot(None)).order_by(AiAssessment.id))).scalars().all():
        if r.kind == "signal_validation":
            ai_sig[r.signal_id] = r          # keep the latest (ordered by id)
        elif r.kind == "execution_review":
            ai_exec[r.signal_id] = r

    claims_by: dict = {}
    if label == B.LABEL_SIGNAL_QUALITY:
        sids = [t.signal_id for t in trades if t.signal_id is not None]
        if sids:
            for c in (await db.execute(select(SignalClaim).where(
                    SignalClaim.signal_id.in_(sids)))).scalars().all():
                claims_by.setdefault(c.signal_id, []).append(c)

    examples = []
    for t in trades:
        sid = t.signal_id
        if sid not in sf_by and sid not in sa_by:   # nothing captured -> skip the row
            continue
        if label == B.LABEL_SIGNAL_QUALITY:
            win = B.signal_quality_label(claims_by.get(sid))
            if win is None:                         # no clean channel outcome -> excluded
                continue
        else:
            win = float(t.realized_pl or 0) > 0
        fv = FV.from_rows(sf_by.get(sid), sa_by.get(sid), ai_sig.get(sid), ai_exec.get(sid))
        examples.append((fv, win))
    return B.build_model(examples, min_n=min_n)


def _round_cond(c: dict) -> dict:
    return {**c, "mean": round(c["mean"], 4), "ci_low": round(c["ci_low"], 4),
            "ci_high": round(c["ci_high"], 4), "raw_wr": round(c["raw_wr"], 4),
            "lift": round(c["lift"], 4)}


@router.get("/bayes")
async def bayes(min_n: int = 5, limit: int = 100, date_from: str = None,
                date_to: str = None, account_id: int | None = None,
                db: AsyncSession = Depends(get_db)):
    """Per-condition Beta-Binomial win-rate table (credible intervals shrink thin
    samples toward the base rate) + Naive-Bayes P(win) for recent signals.
    Optional date range (#58) anchored on trade entry time. `account_id` slices
    the bot-realized label to one account for the per-account A/B (#83); the
    signal-quality label and the feature capture are account-independent."""
    frm, to = parse_iso_utc(date_from), parse_iso_utc(date_to)
    # Two labels (#63): bot_realized (top-level, back-compat) + signal_quality.
    model = await _model(db, min_n, frm, to, label=B.LABEL_BOT_REALIZED, account_id=account_id)
    sq_model = await _model(db, min_n, frm, to, label=B.LABEL_SIGNAL_QUALITY, account_id=account_id)
    tax = await execution_tax_report(db, frm, to, account_id=account_id)

    def _label_block(m):
        if not m.get("ready"):
            return {"ready": False, "n": m.get("n", 0)}
        return {"ready": True, "n": m["n"], "wins": m["wins"], "losses": m["losses"],
                "base_rate": round(m["base_rate"], 4), "min_n": m["min_n"],
                "conditions": [_round_cond(c) for c in m["conditions"][:limit]]}

    if not model.get("ready"):
        return {"ready": False, "n": 0,
                "message": "No closed trades with captured features yet.",
                "labels": {B.LABEL_BOT_REALIZED: _label_block(model),
                           B.LABEL_SIGNAL_QUALITY: _label_block(sq_model)},
                "execution_tax": tax}

    recent = []
    rows = (await db.execute(select(SignalFeature)
                             .order_by(SignalFeature.id.desc()).limit(30))).scalars().all()
    for sf in rows:
        fv = await FV.feature_vector(db, sf.signal_id)      # unified vector (#62)
        sc = B.score(model, fv or {})
        recent.append({"signal_id": sf.signal_id, "symbol": sf.symbol,
                       "direction": sf.direction,
                       "p_win": round(sc["p_win"], 4) if sc else None,
                       "captured_at": sf.captured_at.isoformat() if sf.captured_at else None})

    return {"ready": True, "n": model["n"], "wins": model["wins"],
            "losses": model["losses"], "base_rate": round(model["base_rate"], 4),
            "min_n": model["min_n"],
            "conditions": [_round_cond(c) for c in model["conditions"][:limit]],
            "recent": recent,
            # #63: both labels side-by-side + the per-channel execution tax.
            "labels": {B.LABEL_BOT_REALIZED: _label_block(model),
                       B.LABEL_SIGNAL_QUALITY: _label_block(sq_model)},
            "execution_tax": tax}


async def _bayes_gate_report(db, min_n, frm, to, account_id=None):
    """Would-block vs actual (#64): score every closed labelled trade on the
    SIGNAL-QUALITY model, run the gate ladder to see what it WOULD do (skip /
    de-size / allow / observe), and report each bucket's ACTUAL bot-realized
    win-rate + expectancy. The evidence for going live: the would-skip bucket
    should have materially worse realized expectancy than would-allow. In-sample
    (overfitting caveat — that's why the gate ships log-only until this separates).
    `account_id` slices to one account for the per-account A/B (#83)."""
    cfg = BG.gate_cfg(await get_setting(db, "bayes_gate", None))
    model = await _model(db, min_n, frm, to, label=B.LABEL_SIGNAL_QUALITY, account_id=account_id)
    if not model.get("ready"):
        return {"ready": False, "config": cfg, "acts_live": BG.acts_live(cfg),
                "message": "Signal-quality model not ready (need more labelled trades)."}
    base = model["base_rate"]

    tq = select(Trade).where(Trade.status == "closed")
    if account_id is not None:
        tq = tq.where(Trade.account_id == account_id)
    if frm is not None:
        tq = tq.where(Trade.created_at >= frm)
    if to is not None:
        tq = tq.where(Trade.created_at < to)
    trades = (await db.execute(tq)).scalars().all()
    sf_by = {s.signal_id: s for s in (await db.execute(select(SignalFeature))).scalars().all()}
    sa_by = {s.signal_id: s for s in (await db.execute(select(SignalAnalytics))).scalars().all()}

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}
    buckets = {k: _cell() for k in ("skip", "desize", "allow", "observe")}
    scored = wins_all = 0

    for t in trades:
        sid = t.signal_id
        if t.realized_pl is None or (sid not in sf_by and sid not in sa_by):
            continue
        fv = FV.from_rows(sf_by.get(sid), sa_by.get(sid))
        sc = B.score(model, fv)
        if not sc:
            continue
        ci_low, ci_high = B.score_interval(sc["p_win"], sc["n_eff"], base)
        action, factor, reason = BG.decide(cfg, {"p_win": sc["p_win"], "ci_low": ci_low,
                                                  "ci_high": ci_high, "n": sc["n_eff"]})
        bucket = ("observe" if reason and reason.startswith("observe")
                  else "desize" if (action == "allow" and factor < 1) else action)
        realized = float(t.realized_pl)
        win = realized > 0
        b = buckets[bucket]
        b["n"] += 1
        b["wins"] += 1 if win else 0
        b["pl"] += realized
        scored += 1
        wins_all += 1 if win else 0

    realized_base = (wins_all / scored) if scored else 0.5

    def _fmt(b):
        if not b["n"]:
            return {"n": 0}
        post = B.posterior(b["wins"], b["n"], realized_base)
        return {"n": b["n"], "actual_win_rate": round(b["wins"] / b["n"], 4),
                "actual_expectancy": round(b["pl"] / b["n"], 4),
                "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4)}

    return {"ready": True, "config": cfg, "acts_live": BG.acts_live(cfg),
            "n_scored": scored, "signal_quality_base": round(base, 4),
            "realized_base": round(realized_base, 4),
            "would": {k: _fmt(v) for k, v in buckets.items()},
            "note": ("Log-only shadow (#64). Buckets are what the gate WOULD do; "
                     "figures are the ACTUAL realized outcomes of those trades. "
                     "In-sample — enable live only once would_skip expectancy is "
                     "clearly worse than would_allow at n >= min_trades.")}


@router.get("/bayes-gate/config")
async def bayes_gate_config(db: AsyncSession = Depends(get_db)):
    """The learned-P(win) gate config (#64). Shadow-first: enabled=false,
    mode=log_only by default."""
    return BG.gate_cfg(await get_setting(db, "bayes_gate", None))


@router.put("/bayes-gate/config")
async def bayes_gate_config_put(body: dict, db: AsyncSession = Depends(get_db)):
    cfg = BG.gate_cfg(body)              # sanitize: known keys only
    await set_setting(db, "bayes_gate", cfg)
    return cfg


@router.get("/bayes-gate/report")
async def bayes_gate_report(min_n: int = 5, date_from: str = None, date_to: str = None,
                            account_id: int | None = None,
                            db: AsyncSession = Depends(get_db)):
    return await _bayes_gate_report(db, min_n, parse_iso_utc(date_from),
                                    parse_iso_utc(date_to), account_id=account_id)


@router.get("/bayes/score/{signal_id}")
async def score_signal(signal_id: int, min_n: int = 5, db: AsyncSession = Depends(get_db)):
    model = await _model(db, min_n)
    fv = await FV.feature_vector(db, signal_id)             # unified vector (#62)
    if fv is None:
        raise HTTPException(404, "no captured features for this signal")
    sc = B.score(model, fv)
    return {"signal_id": signal_id, "ready": bool(model.get("ready")),
            "base_rate": round(model.get("base_rate", 0.0), 4) if model.get("ready") else None,
            "score": (None if not sc else
                      {"p_win": round(sc["p_win"], 4), "contributors": sc["contributors"]})}
