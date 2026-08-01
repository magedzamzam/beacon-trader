"""What-if: would we have made money doing it differently? (#183 rebuild)

THE QUESTION THIS ANSWERS, and the only one:

    "I had 100 signals from Quartz Elite. What if we'd filtered by RSI, or
     skipped a session, or exited later — would that have made us profitable?"

It runs the SAME signals twice: once as things actually were (the baseline) and
once with one change applied. Then it says which made more money, in words.

WHAT THIS DELIBERATELY DOES NOT DO. No credible intervals, no best-of-N
inflation, no held-out/in-sample split, no de-lever null. Those live in the
`/analytics/execution-geometry` report and in `metrics.py`, and they are the
right tools for ruling on a live A/B where a wrong call compounds into the
control. They are the wrong tools for "should I try this?", and putting them in
front of that question buries the answer. A what-if is a screening question:
cheap, reversible, and answered by a number and a sentence.

The engine underneath is unchanged — `PortfolioSim`, the real planner, sizing,
staging, sl_rules and risk caps. Only the framing is new.

PURE — stdlib + beacon_core.
"""
from __future__ import annotations

import copy
from typing import List, Optional

# --- the change vocabulary ----------------------------------------------------
# Small on purpose. Every entry is something an operator can say out loud, and
# each maps onto config the engine already understands — nothing here is a new
# execution feature, it is a different arrangement of the existing ones.
EXITS = {
    "be_at_tp1": [
        {"trigger": {"type": "tp_hit", "index": 1},
         "action": {"type": "move_sl_to", "target": "entry"}},
        {"trigger": {"type": "tp_hit", "index": 2},
         "action": {"type": "move_sl_to", "target": "previous_tp"}}],
    "be_at_tp2": [
        {"trigger": {"type": "tp_hit", "index": 2},
         "action": {"type": "move_sl_to", "target": "entry"}},
        {"trigger": {"type": "tp_hit", "index": 3},
         "action": {"type": "move_sl_to", "target": "previous_tp"}}],
    # An EMPTY sl_rules list reads as UNSET and cascades to the default ladder,
    # so "let it run" has to be a rule that can never fire.
    "let_it_run": [
        {"trigger": {"type": "tp_hit", "index": 99},
         "action": {"type": "move_sl_to", "target": "entry"}}],
}

EXIT_LABELS = {
    "be_at_tp1": "move stop to breakeven at TP1",
    "be_at_tp2": "move stop to breakeven at TP2",
    "let_it_run": "never move the stop",
}

# Filters that map onto the shipped `entry_filters` grammar. `mode: live` so they
# actually skip in the simulation — this is a counterfactual, not a shadow.
_TF = "15m"


def _indicator(id_, field, op, value, timeframe=_TF):
    return {"type": "indicator", "id": id_, "timeframe": timeframe,
            "field": field, "op": op, "value": value}


def filter_rule(f: dict) -> Optional[dict]:
    """One what-if filter -> one `entry_filters` rule, or None if it is a
    geometry filter this module applies itself (see `geometry_skip`)."""
    kind = f.get("kind")
    if kind == "rsi_below":
        # We want to SKIP when RSI is at or above the ceiling, so the rule fires
        # on the complement of the condition the operator described.
        return {"enabled": True, "mode": "live", "action": "skip",
                "name": f"RSI at or above {f.get('value')}",
                "when": _indicator("rsi", "value", "gte", f.get("value"))}
    if kind == "rsi_above":
        return {"enabled": True, "mode": "live", "action": "skip",
                "name": f"RSI at or below {f.get('value')}",
                "when": _indicator("rsi", "value", "lte", f.get("value"))}
    if kind == "only_trending":
        return {"enabled": True, "mode": "live", "action": "skip",
                "name": "market not trending",
                "when": {"type": "adx_regime", "timeframe": "4h", "trending": False}}
    if kind == "only_ranging":
        return {"enabled": True, "mode": "live", "action": "skip",
                "name": "market trending",
                "when": {"type": "adx_regime", "timeframe": "4h", "trending": True}}
    if kind == "skip_session":
        return {"enabled": True, "mode": "live", "action": "skip",
                "name": "in " + ", ".join(f.get("sessions") or []),
                "when": {"type": "session_in", "sessions": f.get("sessions") or []}}
    return None


# Filters the live filtration engine has no `when.type` for, so the harness has
# to apply them on the signal set itself. Named rather than inlined so the page's
# offered filters can be checked against the union of both paths.
GEOMETRY_KINDS = frozenset({"min_stop_atr"})

# The timeframe the staged engine's ATR comes from live, so "1x ATR" here means
# the same thing an operator reading #189 thinks it means.
ATR_TIMEFRAME = "1h"


def geometry_skip(f: dict, signal, atr_abs) -> bool:
    """Filters the engine has no evaluator for, applied here on the signal set.

    `min_stop_atr` is the big one: sub-ATR stops were the single largest R leak
    measured on the control arm (#189), and there is no `when.type` for stop
    geometry in the live filtration engine. Doing it here keeps the trading path
    untouched — this is a replay-only question."""
    if f.get("kind") not in GEOMETRY_KINDS or atr_abs in (None, 0):
        return False
    try:
        dist = abs(float(signal.parsed.entry_to) - float(signal.parsed.sl))
        return (dist / float(atr_abs)) < float(f.get("value") or 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def describe(changes: dict) -> str:
    """The change, in one human phrase. Used as the what-if column header and in
    the verdict, so the reader never has to decode a config to know what was
    tested."""
    bits = []
    for f in (changes.get("filters") or []):
        k, v = f.get("kind"), f.get("value")
        bits.append({
            "rsi_below": f"only take signals with RSI below {v}",
            "rsi_above": f"only take signals with RSI above {v}",
            "only_trending": "only trade when the market is trending",
            "only_ranging": "only trade when the market is ranging",
            "skip_session": "skip " + ", ".join(f.get("sessions") or []),
            "min_stop_atr": f"skip signals whose stop is under {v}x ATR",
        }.get(k, str(k)))
    if changes.get("exit") in EXIT_LABELS:
        bits.append(EXIT_LABELS[changes["exit"]])
    if changes.get("risk_percent"):
        bits.append(f"risk {changes['risk_percent']}% per trade")
    if changes.get("entry_style"):
        bits.append(f"{changes['entry_style']} entries")
    return " + ".join(bits) if bits else "no change"


def apply_changes(variant: dict, changes: dict) -> dict:
    """A live variant + the operator's change = the what-if arm."""
    v = copy.deepcopy(variant)
    rules = [r for r in (filter_rule(f) for f in (changes.get("filters") or []))
             if r]
    exit_rules = EXITS.get(changes.get("exit"))
    for st in v.get("strategies", []):
        base = st.get("account_id") is None and st.get("source_id") is None
        if rules:
            ef = st.setdefault("entry_filters", {})
            # Replace rather than append: the operator is asking "what if we
            # filtered by THIS", not "what if we added it on top of whatever is
            # already there and could not see".
            ef["rules"] = copy.deepcopy(rules) if base else []
        if exit_rules is not None:
            ep = st.setdefault("exit_policy", {})
            if base:
                ep["sl_rules"] = copy.deepcopy(exit_rules)
            else:
                ep.pop("sl_rules", None)     # let the base layer win uniformly
        if changes.get("entry_style"):
            st.setdefault("entry_policy", {})["entry_style"] = changes["entry_style"]
    if changes.get("risk_percent"):
        v.setdefault("risk", {})["default"] = {
            "basis": "capital_percent", "value": float(changes["risk_percent"]),
            "allocation": "even"}
    return v


# --- reading the outcome ------------------------------------------------------
# How price moved, in the three shapes a person actually asks about. Thresholds
# are stated rather than tuned: a loser that never got 0.3R our way was never
# working, and one that got a full R before reversing is a different problem
# (the exit) from one that never moved (the entry).
STRAIGHT_TO_SL = 0.3
WENT_OUR_WAY = 1.0


def _travel(t) -> str:
    entry = float(t.legs[0].fill_price or t.legs[0].entry) if t.legs else None
    stop = float(t.initial_sl) if t.initial_sl is not None else None
    if entry is None or stop is None or t.mfe is None or entry == stop:
        return "unknown"
    r = (float(t.mfe) - entry) / abs(entry - stop)
    if t.direction == "SELL":
        r = -r
    if r >= WENT_OUR_WAY:
        return "went_our_way_then_reversed" if float(t.realized_pl) <= 0 else "ran_to_target"
    if r < STRAIGHT_TO_SL:
        return "straight_to_sl"
    return "ranged"


def summarise(res, *, label: str) -> dict:
    """Plain counts for one arm. Everything here is something a person asked for
    in the brief — signals, executed, skipped and why, money, the TP ladder, and
    how price actually moved."""
    trades = list(res.trades)
    filled = [t for t in trades if t.ever_filled]
    pl = sum(float(t.realized_pl) for t in filled)
    wins = sum(1 for t in filled if float(t.realized_pl) > 0)

    # COUNTED PER TRADE, not per leg. A staged entry is several legs on one
    # signal, so counting legs reported 172 stop-outs against 78 executed
    # trades — a number that cannot be read, next to one that can.
    #
    # Cumulative, because that is how the ladder is read out loud: a trade that
    # reached TP2 also reached TP1. And `stopped_out` means the trade reached NO
    # target and closed at the stop — a trade that banked TP1 and then stopped
    # the runner is not what anyone means by "stopped out".
    tp = {1: 0, 2: 0, 3: 0}
    stopped = 0
    for t in filled:
        best = max([leg.tp_index or 0 for leg in t.legs
                    if leg.outcome == "tp_hit"] or [0])
        for i in tp:
            if best >= i:
                tp[i] += 1
        if not best and any(leg.outcome in ("sl_hit", "breakeven")
                            for leg in t.legs):
            stopped += 1

    reasons = {}
    for nt in res.not_taken:
        reasons[str(nt.get("reason"))] = reasons.get(str(nt.get("reason")), 0) + 1
    filtered = sum(v for k, v in reasons.items()
                   if k.startswith("filtration") or k.startswith("whatif"))
    never_filled = len(trades) - len(filled)

    travel = {}
    for t in filled:
        k = _travel(t)
        travel[k] = travel.get(k, 0) + 1

    return {
        "label": label,
        "signals": len(trades) + len(res.not_taken),
        "executed": len(filled),
        "skipped": len(res.not_taken) + never_filled,
        "skipped_by_rule": filtered,
        "skipped_no_fill": never_filled,
        "skipped_other": len(res.not_taken) - filtered,
        "profit": round(pl, 2),
        "wins": wins,
        "losses": len(filled) - wins,
        "tp1": tp[1], "tp2": tp[2], "tp3": tp[3],
        "stopped_out": stopped,
        "travel": travel,
        "skip_reasons": reasons,
    }


# --- what the answer cannot tell you ------------------------------------------
# A signal's timestamp is its INGEST time. When a channel is onboarded its
# backlog arrives at once, so a block of signals carries one moment — and every
# indicator filter evaluates all of them against the same bar. Measured on this
# book: 179 of 856 signals sit in `2026-07-05 16:30`, the onboarding backfill.
#
# Left unsaid, that reads as "the RSI filter barely did anything" when the truth
# is "a fifth of your history has no usable time for an indicator to be read at".
# The filter is not broken and the number is not wrong; the question is partly
# unanswerable on that block, which is a different thing and has to be said.
BULK_BAR_MINUTES = 15
BULK_MIN_SIGNALS = 10


def _bar_key(at):
    return at.replace(minute=(at.minute // BULK_BAR_MINUTES) * BULK_BAR_MINUTES,
                      second=0, microsecond=0)


def bulk_ingest_caveat(signals, changes: dict) -> Optional[str]:
    """Plain-words warning when a filter cannot see the signals it is judging."""
    if not (changes.get("filters") or []):
        return None                        # no filter, no timing dependence
    counts = {}
    for s in signals:
        try:
            counts[_bar_key(s.at)] = counts.get(_bar_key(s.at), 0) + 1
        except (AttributeError, TypeError, ValueError):
            continue
    blocks = {k: n for k, n in counts.items() if n >= BULK_MIN_SIGNALS}
    n = sum(blocks.values())
    if not n:
        return None
    worst = max(blocks, key=blocks.get)
    return (f"{n} of these {len(signals)} signals arrived in "
            f"{len(blocks)} burst(s) — {blocks[worst]} of them in the single "
            f"{BULK_BAR_MINUTES}-minute window at "
            f"{worst.strftime('%Y-%m-%d %H:%M')}, which is when the channel was "
            "onboarded and its backlog was imported. A filter reads the market "
            "at the signal's timestamp, so for those it reads ONE moment and "
            "can only keep or drop the whole block. Treat the filter's effect on "
            "them as unmeasured rather than small.")


def verdict(base: dict, alt: dict, changes: dict) -> dict:
    """One sentence a person can act on, plus the two numbers behind it."""
    delta = round(alt["profit"] - base["profit"], 2)
    removed = alt["skipped_by_rule"] - base["skipped_by_rule"]
    lost_winners = max(0, base["wins"] - alt["wins"])
    change = describe(changes)

    if delta > 0:
        head = f"Better by {delta:,.2f}."
    elif delta < 0:
        head = f"Worse by {abs(delta):,.2f}."
    else:
        head = "No difference."

    parts = [head]
    if removed > 0:
        parts.append(f"The change skipped {removed} signal(s) you took before.")
        if lost_winners:
            parts.append(f"{lost_winners} of them were winners.")
    elif removed < 0:
        parts.append(f"It took {abs(removed)} signal(s) you skipped before.")
    if base["executed"] and alt["executed"] == 0:
        parts.append("It skipped EVERYTHING — the filter is too strict to test.")
    elif changes.get("filters") and removed <= 0:
        # Measured: an RSI-below-70 filter touched 2 of 114 Quartz signals,
        # because RSI is rarely that high when these channels post. The delta
        # was +80 on a -1,189 book, and reading that as "the filter helps" is
        # reading noise. Say the filter barely applied instead.
        parts.append("The filter barely applied — it changed almost nothing, so "
                     "this says little either way. Try a tighter threshold.")
    elif base["executed"] and removed >= 0.9 * base["executed"]:
        parts.append("It removed nearly everything, so what is left is a handful "
                     "of survivors rather than a strategy.")
    if alt["executed"] and alt["executed"] < 10:
        parts.append(f"Only {alt['executed']} trades left, so treat this as a hint "
                     "rather than an answer.")
    return {
        "better": delta > 0,
        "delta": delta,
        "change": change,
        "headline": " ".join(parts),
        "baseline_profit": base["profit"],
        "whatif_profit": alt["profit"],
    }


def report(base_res, alt_res, *, changes: dict, scope_label: str,
           frm=None, to=None, signals=()) -> dict:
    """The whole answer: two columns, a verdict, and what it cannot tell you."""
    base = summarise(base_res, label="What happened")
    alt = summarise(alt_res, label="What-if: " + describe(changes))
    caveats = [c for c in (bulk_ingest_caveat(signals, changes),) if c]
    return {
        "scope": scope_label,
        "from": frm.isoformat() if frm is not None else None,
        "to": to.isoformat() if to is not None else None,
        "change": describe(changes),
        "baseline": base,
        "whatif": alt,
        "verdict": verdict(base, alt, changes),
        "caveats": caveats,
        "note": ("Same signals, run twice. This is a screening question — it says "
                 "whether a change is worth a real test, not whether to make it. "
                 "A live frozen-week A/B is still what promotes a config."),
    }
