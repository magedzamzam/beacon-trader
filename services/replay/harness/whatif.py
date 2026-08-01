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


def _is_rule(reason: str) -> bool:
    return reason.startswith("filtration") or reason.startswith("whatif")


def summarise(res, *, label: str) -> dict:
    """Plain counts for one arm, COUNTED PER SIGNAL.

    The operator's question is "I had 100 signals from Quartz Elite — what if
    we'd filtered by RSI?", so every count here is a count of SIGNALS.

    That is not what the simulator produces. It emits one row per (signal,
    account), and this book fans one signal out to three accounts — so summing
    its rows reported 492 signals for a channel that sent 170. The caveat line
    right underneath said 170, which is how it was caught.

    Money is the exception and stays a total across accounts, because "would
    that have made us profitable" is a question about the book, not about one
    account's share of it."""
    by_sig, skips = {}, {}
    for t in res.trades:
        by_sig.setdefault(t.signal_id, []).append(t)
    for nt in res.not_taken:
        skips.setdefault(nt.get("signal_id"), []).append(str(nt.get("reason")))

    # Filled ANYWHERE counts as executed: the signal was traded, even if one
    # account's risk cap turned it away.
    executed = {sid for sid, ts in by_sig.items() if any(t.ever_filled for t in ts)}
    all_ids = set(by_sig) | set(skips)
    skipped = all_ids - executed

    by_rule = sum(1 for sid in skipped if any(_is_rule(r) for r in skips.get(sid, ())))
    no_fill = sum(1 for sid in skipped
                  if not any(_is_rule(r) for r in skips.get(sid, ()))
                  and by_sig.get(sid))
    other = len(skipped) - by_rule - no_fill

    # Money over every filled row; the ladder and the travel per SIGNAL, taking
    # the best any account did with it — the geometry is identical across
    # accounts, only the sizing differs.
    #
    # Cumulative, because that is how a ladder is read out loud: a signal that
    # reached TP2 also reached TP1. And `stopped_out` means it reached NO target
    # — a signal that banked TP1 and then stopped the runner is not what anyone
    # means by "stopped out".
    pl, wins, losses, stopped = 0.0, 0, 0, 0
    tp = {1: 0, 2: 0, 3: 0}
    travel = {}
    for sid in executed:
        ts = [t for t in by_sig[sid] if t.ever_filled]
        money = sum(float(t.realized_pl) for t in ts)
        pl += money
        if money > 0:
            wins += 1
        else:
            losses += 1
        best = max([leg.tp_index or 0 for t in ts for leg in t.legs
                    if leg.outcome == "tp_hit"] or [0])
        for i in tp:
            if best >= i:
                tp[i] += 1
        if not best and any(leg.outcome in ("sl_hit", "breakeven")
                            for t in ts for leg in t.legs):
            stopped += 1
        k = _travel(ts[0])
        travel[k] = travel.get(k, 0) + 1

    reasons = {}
    for rs in skips.values():
        for r in rs:
            reasons[r] = reasons.get(r, 0) + 1

    return {
        "label": label,
        "signals": len(all_ids),
        "executed": len(executed),
        "skipped": len(skipped),
        "skipped_by_rule": by_rule,
        "skipped_no_fill": no_fill,
        "skipped_other": other,
        "profit": round(pl, 2),
        "wins": wins,
        "losses": losses,
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


def _bursts(signals) -> dict:
    counts = {}
    for s in signals:
        try:
            counts[_bar_key(s.at)] = counts.get(_bar_key(s.at), 0) + 1
        except (AttributeError, TypeError, ValueError):
            continue
    return {k: n for k, n in counts.items() if n >= BULK_MIN_SIGNALS}


def bulk_ingest_caveats(signals, changes: dict) -> List[str]:
    """What this answer cannot tell you, in plain words.

    TWO separate problems, and they need saying separately because one is about
    the change and the other is about the baseline.

    Measured on this book: 179 of 856 signals sit in the single 15-minute window
    2026-07-05 16:30, and NONE of those 179 produced a real trade. They are the
    onboarding backlog, imported at once and never executed."""
    blocks = _bursts(signals)
    n = sum(blocks.values())
    if not n:
        return []
    worst = max(blocks, key=blocks.get)
    when = worst.strftime("%Y-%m-%d %H:%M")
    out = [
        # ALWAYS. This one is about the left column, so it does not depend on
        # what was changed. Both arms simulate these signals; the account did
        # not trade them.
        f"{n} of these {len(signals)} signals arrived in "
        + (f"one burst — all {blocks[worst]} of them in the "
           if len(blocks) == 1 else
           f"{len(blocks)} bursts — {blocks[worst]} of them in a single ")
        + f"{BULK_BAR_MINUTES}-minute window at {when}, which is when the "
        "channel was onboarded and its backlog was imported. Both columns "
        "SIMULATE those signals; they are not a replay of your account "
        "statement.",
    ]
    if changes.get("filters"):
        # Only for filters: an exit ladder is evaluated bar by bar AFTER entry,
        # so a clustered ingest time does not blind it.
        out.append(
            "A filter reads the market at the signal's timestamp, so for those "
            f"{n} it reads ONE moment and can only keep or drop the whole block. "
            "Treat the filter's effect on them as unmeasured rather than small.")
    return out


# The TP index each named exit ratchets on. A ratchet at TP N only protects
# something if a leg is still open AFTER TP N closes — i.e. the signal posted
# more than N targets.
EXIT_TRIGGER = {"be_at_tp1": 1, "be_at_tp2": 2}


def exit_reach_caveat(signals, changes: dict) -> Optional[str]:
    """Say when the chosen exit could not fire on the signals it was tested on.

    MEASURED: all 114 Quartz Elite signals post exactly 2 targets, so
    `be_at_tp2` and `let_it_run` returned byte-identical results — TP2 closes
    the last leg and there is nothing left to move a stop on. The run was really
    measuring "stop ratcheting at TP1", and the verdict named the wrong cause.

    A change that cannot fire is the silent-no-op failure this module exists to
    refuse: nothing errors, the numbers move for a different reason, and the
    operator acts on an attribution that is wrong."""
    idx = EXIT_TRIGGER.get(changes.get("exit"))
    if not idx:
        return None
    depths = []
    for s in signals:
        try:
            depths.append(len(s.parsed.tps or ()))
        except AttributeError:
            continue
    if not depths:
        return None
    unreachable = sum(1 for d in depths if d <= idx)
    if unreachable < 0.9 * len(depths):
        return None
    label = EXIT_LABELS.get(changes["exit"], changes["exit"])
    every = ("Every one of these" if unreachable == len(depths)
             else f"{unreachable} of these")
    target = "target" if idx == 1 else "targets"
    return (f"{every} {len(depths)} signals posts {idx} {target} at most, so "
            f"\"{label}\" never has a leg left to protect. Any difference you "
            "see below comes from REMOVING the exit you run today, not from "
            "adding this one.")


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
    # NOT "what happened". The left column is a SIMULATION of the current setup
    # over these signals, and on this book 179 of 856 signals were never traded
    # live at all. Calling it "what happened" put a claim on it that the number
    # cannot support — the Reconciler exists precisely because sim and broker
    # truth differ (agreement 0.9149, #187).
    base = summarise(base_res, label="Your setup now")
    alt = summarise(alt_res, label="What-if: " + describe(changes))
    caveats = bulk_ingest_caveats(signals, changes)
    reach = exit_reach_caveat(signals, changes)
    if reach:
        caveats.insert(0, reach)      # it changes what the numbers MEAN
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
