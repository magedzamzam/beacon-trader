"""Bayesian correlation of signal-time TA features with trade outcomes.

Two products, both from the same labelled set of (features, win) pairs:
  1. A per-condition Beta-Binomial posterior win-rate with a credible interval
     (small samples shrink toward the base rate — 2/2 is NOT reported as 100%).
  2. A Naive-Bayes P(win | features) score for a signal, learned from history.

Win is defined by the caller (we use realized P&L > 0). Pure Python.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

_FPMIN = 1e-300
_EPS = 3e-12

# Selectable learning targets (#63): the channel's own signal-quality outcome vs
# our bot's execution outcome. The gap between them per channel is the execution tax.
LABEL_BOT_REALIZED = "bot_realized"       # trade.realized_pl > 0 (execution outcome)
LABEL_SIGNAL_QUALITY = "signal_quality"   # channel reached TP1+ vs SL (setup outcome)
LABELS = (LABEL_BOT_REALIZED, LABEL_SIGNAL_QUALITY)


def time_link_confidence(gap_hours: float, max_hours: int) -> float:
    """Confidence of a time-proximity claim→signal link (#63): decays linearly
    from 0.7 (immediate follow-up) to 0.3 (at the max_hours edge). A direct
    Telegram reply link scores 1.0 (assigned by the reconciler, not here). Pure."""
    span = max(1, max_hours)
    return round(max(0.3, 0.7 - 0.4 * (max(0.0, gap_hours) / span)), 3)


def signal_quality_label(claims, *, min_confidence: float = 0.0) -> Optional[bool]:
    """The channel's OWN signal-quality outcome from its claim rows (#63), a label
    independent of our execution (fills, stops, TTL). `claims` is the list of
    SignalClaim-like rows for ONE signal, each exposing max_tp_claimed:int,
    sl_claimed:bool, all_tp:bool, claim_confidence:float|None.

      win  (True)  = the channel claimed TP1+ reached (the setup worked)
      loss (False) = SL claimed with no TP reached (the setup failed)
      None         = no actionable claim, OR contradictory claims (all-TP AND SL)
                     — excluded from the label, never counted as a loss.

    `min_confidence` drops weakly-linked claims (a time-proximity match rather than
    a reply) before labelling — the excludes-low-confidence-links guard. A NULL
    confidence (pre-#63 row) is treated as unknown and NOT excluded, so historical
    data isn't silently dropped."""
    usable = [c for c in (claims or [])
              if (getattr(c, "claim_confidence", None) is None
                  or float(getattr(c, "claim_confidence")) >= min_confidence)]
    if not usable:
        return None
    max_tp = max((int(getattr(c, "max_tp_claimed", 0) or 0) for c in usable), default=0)
    all_tp = any(bool(getattr(c, "all_tp", False)) for c in usable)
    sl = any(bool(getattr(c, "sl_claimed", False)) for c in usable)
    if all_tp and sl:
        return None                       # contradictory -> ambiguous, exclude
    if max_tp >= 1 or all_tp:
        return True                       # reached TP1+ -> quality win
    if sl:
        return False                      # SL, no TP -> quality loss
    return None                           # nothing actionable claimed -> exclude


# ---- regularized incomplete beta + inverse (for credible intervals) -------
def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < _EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) = P(Beta(a,b) <= x)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(p: float, a: float, b: float) -> float:
    """Inverse CDF (quantile) of Beta(a,b) via bisection on betainc."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def posterior(wins: int, n: int, base_rate: float,
              prior_strength: float = 20.0, cred: float = 0.90) -> dict:
    """Beta-Binomial posterior win-rate. Prior mean = base_rate, weight =
    prior_strength (pseudo-observations), so thin conditions are pulled toward
    the base rate with a wide interval."""
    a0 = max(1e-6, base_rate * prior_strength)
    b0 = max(1e-6, (1.0 - base_rate) * prior_strength)
    a, b = a0 + wins, b0 + (n - wins)
    mean = a / (a + b)
    tail = (1.0 - cred) / 2.0
    return {"mean": mean, "ci_low": beta_ppf(tail, a, b),
            "ci_high": beta_ppf(1.0 - tail, a, b)}


# ---- feature flattening + conditions --------------------------------------
def _flatten(features: dict) -> Dict[str, object]:
    """{tf: {indicator_key: {field: value}}} -> {'tf.key.field': value}.

    Also accepts an ALREADY-flat namespaced dict (the unified feature vector,
    #62): a scalar top-level value is passed through unchanged (e.g.
    'analytics.regime.label' -> 'bull'), so both shapes flatten idempotently."""
    out: Dict[str, object] = {}
    for tf, inds in (features or {}).items():
        if not isinstance(inds, dict):
            if not str(tf).startswith("_"):
                out[tf] = inds                       # already-flat entry, pass through
            continue
        for key, val in inds.items():
            if key.startswith("_"):
                continue
            if isinstance(val, dict):
                for field, v in val.items():
                    out[f"{tf}.{key}.{field}"] = v
            else:
                out[f"{tf}.{key}"] = val
    return out


def _conditions(flat: Dict[str, object], thresholds: Dict[str, Tuple[float, float]]) -> set:
    """Turn a flattened example into discrete condition tokens."""
    cs = set()
    for path, val in flat.items():
        if isinstance(val, bool):
            cs.add(f"{path}={'yes' if val else 'no'}")
        elif isinstance(val, (int, float)):
            th = thresholds.get(path)
            if th is not None:
                lo, hi = th
                bucket = "low" if val <= lo else ("high" if val > hi else "mid")
                cs.add(f"{path}:{bucket}")
        elif val is None:
            continue
        else:
            cs.add(f"{path}={val}")
    return cs


def build_model(examples: List[Tuple[dict, bool]], *, min_n: int = 5,
                prior_strength: float = 20.0, cred: float = 0.90) -> dict:
    """examples: (features_dict, win_bool). Returns a model with the
    per-condition posterior table and everything needed to score new signals."""
    n = len(examples)
    if n == 0:
        return {"ready": False, "n": 0}
    wins = sum(1 for _, w in examples if w)
    base = wins / n

    flats = [(_flatten(f), w) for f, w in examples]

    # tercile thresholds per numeric path (p33 / p66)
    numeric: Dict[str, List[float]] = {}
    for flat, _ in flats:
        for path, val in flat.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                numeric.setdefault(path, []).append(float(val))
    thresholds: Dict[str, Tuple[float, float]] = {}
    for path, vals in numeric.items():
        if len(vals) >= min_n:
            s = sorted(vals)
            thresholds[path] = (s[len(s) // 3], s[2 * len(s) // 3])

    cond_w: Dict[str, int] = {}
    cond_n: Dict[str, int] = {}
    for flat, w in flats:
        for c in _conditions(flat, thresholds):
            cond_n[c] = cond_n.get(c, 0) + 1
            if w:
                cond_w[c] = cond_w.get(c, 0) + 1

    table = []
    for c, cn in cond_n.items():
        if cn < min_n:
            continue
        cw = cond_w.get(c, 0)
        post = posterior(cw, cn, base, prior_strength, cred)
        table.append({"condition": c, "n": cn, "wins": cw, "raw_wr": cw / cn,
                      "mean": post["mean"], "ci_low": post["ci_low"],
                      "ci_high": post["ci_high"], "lift": post["mean"] - base})
    # most reliably-better first: highest lower credible bound
    table.sort(key=lambda r: r["ci_low"], reverse=True)

    return {"ready": True, "n": n, "wins": wins, "losses": n - wins,
            "base_rate": base, "thresholds": thresholds, "cond_w": cond_w,
            "cond_n": cond_n, "min_n": min_n, "conditions": table}


def score(model: dict, features: dict) -> Optional[dict]:
    """Naive-Bayes P(win | features) from a built model, with the top
    contributing conditions (by log-likelihood-ratio)."""
    if not model.get("ready"):
        return None
    tw, tl, base = model["wins"], model["losses"], model["base_rate"]
    if tw == 0 or tl == 0 or not (0.0 < base < 1.0):
        return None
    flat = _flatten(features)
    logit = math.log(base / (1.0 - base))
    contribs = []
    for c in _conditions(flat, model["thresholds"]):
        cn = model["cond_n"].get(c, 0)
        if cn < model["min_n"]:
            continue
        cw = model["cond_w"].get(c, 0)
        cl = cn - cw
        p_c_w = (cw + 1) / (tw + 2)       # Laplace-smoothed likelihoods
        p_c_l = (cl + 1) / (tl + 2)
        lr = math.log(p_c_w / p_c_l)
        logit += lr
        contribs.append({"condition": c, "lr": lr, "n": cn})
    p = 1.0 / (1.0 + math.exp(-logit))
    contribs.sort(key=lambda x: abs(x["lr"]), reverse=True)
    # Effective sample size behind this score = the smallest evidence among the
    # matched conditions (the weakest link), or the full model when only the base
    # rate applies. Drives the per-signal credible interval the #64 gate acts on.
    n_eff = min((c["n"] for c in contribs), default=model.get("n", 0))
    return {"p_win": p, "n_eff": n_eff, "contributors": contribs[:8]}


def score_interval(p_win: float, n_eff: int, base_rate: float,
                   cred: float = 0.90) -> Tuple[float, float]:
    """A per-signal credible interval for p_win (#64): a Beta posterior centred on
    p_win with weight = the effective sample size behind the score, so a p_win
    resting on thin evidence gets a wide interval the gate must respect."""
    n = max(0, int(n_eff))
    wins = int(round(max(0.0, min(1.0, p_win)) * n))
    post = posterior(wins, n, base_rate, cred=cred)
    return post["ci_low"], post["ci_high"]
