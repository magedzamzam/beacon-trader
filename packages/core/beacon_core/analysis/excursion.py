"""Exit-independent signal labelling: MFE / MAE / R-ladder from 1m bid-ask
candles (#182).

Every outcome label mined so far is contaminated by how we exited.
`bot_realized` is `trade.realized_pl > 0`, which bakes in the BE@TP1 ratchet;
`signal_quality` depends on a channel reporting its own result. Worse, per-channel
win rates are confounded by TP GEOMETRY: median TP1 sits at 0.15R for TFXC and
1.00R for Quartz, a 6.7x difference in how far price must move to "win". Any
binary TP1-vs-SL rate compares those channels on unequal terms.

Replaying the bars answers the question the label should have been asking all
along: **given the signal, how far did price travel in the called direction
before the ORIGINAL stop was hit?** Where to take profit is then an execution
decision made against that curve, not a property of the signal.

PURE — stdlib only, no DB / clock / broker, so it runs on a bare box and #169's
replay harness can reuse the same sided-price semantics rather than fork them.

Sided price semantics (must match #169's candle schema):
  BUY  entered at ask, exited by SELLING at the bid -> favourable `high_bid`,
       adverse `low_bid`.
  SELL entered at bid, exited by BUYING at the ask  -> favourable `low_ask`,
       adverse `high_ask`.

Two conservative choices, both deliberate, both counted and reported rather than
buried:
  1. **Same-bar TP/SL ambiguity.** A 1m bar that touches both cannot say which
     came first, so it is scored as the STOP, and the bar does not extend MFE.
  2. **The original stop.** MFE is measured against the immutable signal SL, never
     a ratcheted one — using the moved stop would let the exit strategy truncate
     the excursion window and re-contaminate the label this module exists to
     decontaminate.
"""
from __future__ import annotations

from typing import Optional, Sequence

# P(price reaches X*R in the called direction before the original stop). 0.25R is
# below every channel's TP1; 3R is beyond every ladder we place.
LADDER = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)

# A signal that resolves neither way must still terminate. 1440 1m bars = 24h.
DEFAULT_HORIZON_BARS = 1440

RACE_TP1 = "tp1"
RACE_SL = "sl"
RACE_HORIZON = "horizon"


def _px(bar, name: str) -> Optional[float]:
    """One sided price off a bar, accepting a mapping, a row tuple-like, or an
    ORM object — the store hands us column tuples, tests hand us dicts."""
    v = bar.get(name) if isinstance(bar, dict) else getattr(bar, name, None)
    return None if v is None else float(v)


def _sides(direction: str) -> tuple[str, str, int]:
    """(favourable column, adverse column, sign) for a direction. `sign` maps a
    price into favourable-distance space: +1 for BUY (up is good), -1 for SELL."""
    if str(direction).upper() == "SELL":
        return "low_ask", "high_ask", -1
    return "high_bid", "low_bid", 1


def excursion(bars: Sequence, *, direction: str, entry: float, sl: float,
              tp1: Optional[float] = None, ladder: Sequence[float] = LADDER,
              horizon_bars: int = DEFAULT_HORIZON_BARS) -> Optional[dict]:
    """Replay `bars` (ordered, starting at or after the entry) and reconstruct the
    excursion. Returns None when the geometry is undefined (no bars, or SL at the
    entry, which makes R zero and every ratio infinite).

    Keys:
      `r`            price distance of one R (|entry - sl|)
      `tp1_r`        how far TP1 sits in R — the confound this module measures
      `mfe_r`/`mae_r`  furthest favourable / adverse excursion, in R. MFE stops at
                     the SL bar; MAE stops once TP1 is reached. Both may be
                     negative when price never traded that side.
      `ladder`       {"0.5": True, …} — reached X*R before the stop
      `race`         tp1 | sl | horizon (which came first)
      `bars_to_tp1` / `bars_to_sl`  bar index, 0 = the entry bar
      `same_bar_ambiguous`  one bar touched both TP1 and SL -> scored as the stop
      `resolved` / `horizon_capped`  did it terminate, or run out of horizon
    """
    if not bars:
        return None
    entry, sl = float(entry), float(sl)
    r = abs(entry - sl)
    if r <= 0:
        return None
    fav_col, adv_col, sign = _sides(direction)

    # TP1 on the wrong side of entry is bad data, not a target — ignore it rather
    # than report a negative distance that would read as "an easy TP".
    tp1_dist = None
    if tp1 is not None:
        d = sign * (float(tp1) - entry)
        if d > 0:
            tp1_dist = d

    window = list(bars)[:max(1, int(horizon_bars))]
    mfe = mae = None
    bars_to_tp1 = bars_to_sl = None
    same_bar = False
    mae_open = True                     # MAE accumulates until TP1 is reached

    for i, bar in enumerate(window):
        fav_px, adv_px = _px(bar, fav_col), _px(bar, adv_col)
        if fav_px is None or adv_px is None:
            continue                    # a hole in the bar, not a data point
        fav = sign * (fav_px - entry)   # favourable distance this bar reached
        adv = sign * (entry - adv_px)   # adverse distance this bar reached

        if mae_open:
            # On the TP1 bar the conservative order is adverse-first, so this
            # bar's adverse travel still counts before the window closes.
            mae = adv if mae is None else max(mae, adv)

        tp1_here = tp1_dist is not None and fav >= tp1_dist
        sl_here = adv >= r

        if sl_here:
            if tp1_here:
                same_bar = True         # unresolvable within a 1m bar
            bars_to_sl = i
            break                       # and the bar does NOT extend MFE

        mfe = fav if mfe is None else max(mfe, fav)
        if tp1_here:
            if bars_to_tp1 is None:
                bars_to_tp1 = i
            mae_open = False

    if mfe is None and mae is None:
        return None                     # every bar in the window was unusable

    mfe = 0.0 if mfe is None else mfe   # stopped out on the entry bar
    mae = 0.0 if mae is None else mae
    mfe_r, mae_r = mfe / r, mae / r

    if bars_to_tp1 is not None:
        race = RACE_TP1
    elif bars_to_sl is not None:
        race = RACE_SL
    else:
        race = RACE_HORIZON

    return {
        "r": r,
        "tp1_r": None if tp1_dist is None else tp1_dist / r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "ladder": {_lkey(x): mfe_r >= x for x in ladder},
        "race": race,
        "bars_to_tp1": bars_to_tp1,
        "bars_to_sl": bars_to_sl,
        "same_bar_ambiguous": same_bar,
        "resolved": race != RACE_HORIZON,
        "horizon_capped": race == RACE_HORIZON,
        "n_bars": len(window),
    }


def _lkey(x: float) -> str:
    """Ladder rung as a stable JSON key: 0.5 -> "0.5", 1.0 -> "1.0"."""
    return f"{float(x):g}" if float(x) % 1 else f"{float(x):.1f}"


# --- the label -----------------------------------------------------------------
DEFAULT_LABEL_R = 1.0


def excursion_label(row, *, min_r: float = DEFAULT_LABEL_R) -> Optional[bool]:
    """Did the market offer `min_r` in the called direction before the original
    stop? Exit-independent by construction, and defined for EVERY signal with
    candle coverage — including ones we skipped, filtered or never filled, which
    is the only way to score what a filter rejected.

    True/False is the honest reading even for a horizon-capped signal: it did not
    offer min_r inside the horizon. None means no reconstruction (no coverage),
    which is an exclusion, never a loss.

    Note the horizon is the label's ceiling: widening it can only turn a False
    into a True, so a run must report its horizon alongside the rate."""
    if row is None:
        return None
    mfe = row.get("mfe_r") if isinstance(row, dict) else getattr(row, "mfe_r", None)
    return None if mfe is None else float(mfe) >= float(min_r)
