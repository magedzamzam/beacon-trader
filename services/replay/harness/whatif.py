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

from . import provenance as P

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

# --- entry: any condition the engine can evaluate -----------------------------
# The operator states what must be TRUE to take the trade. Anything in the TA
# registry (45 indicators, FVG and order blocks included), plus sessions and the
# ADX regime read. Nothing here is a new execution feature — it is the shipped
# `entry_filters` grammar with a form in front of it.
_TF = "15m"

OP_WORDS = {
    "lt": "is below", "lte": "is at or below",
    "gt": "is above", "gte": "is at or above",
    "eq": "is", "ne": "is not",
    "is_true": "is there", "is_false": "is not there",
    "between": "is between", "outside": "is outside",
}
BOOL_OPS = ("is_true", "is_false")


def _label(indicator_id: str) -> str:
    """The registry's own display name, so the sentence the operator reads back
    matches the one they built."""
    try:
        from beacon_core.ta import registry as TA
        for spec in TA.catalog()["indicators"]:
            if spec["id"] == indicator_id:
                return spec["label"]
    except Exception:                            # never break a report on a label
        pass
    return str(indicator_id)


def keep_leaf(c: dict) -> Optional[dict]:
    """One operator condition -> one engine condition leaf.

    These are KEEP conditions ("only trade when..."), not skip rules. They are
    composed into a single `not(all(...))` skip in `entry_rules`, which is what
    makes an arbitrary number of them behave: Kleene's `not UNKNOWN` is UNKNOWN,
    so a condition whose indicator could not be computed still fails OPEN and
    the signal is taken. A per-condition skip rule would have to invert each
    operator by hand, and inverting `between` or a boolean field is exactly the
    kind of thing that silently filters the wrong half of the book."""
    kind = c.get("kind")
    if kind == "indicator":
        if not c.get("id") or not c.get("op"):
            return None
        leaf = {"type": "indicator", "id": c["id"],
                "timeframe": c.get("timeframe") or _TF,
                "field": c.get("field") or "value", "op": c["op"]}
        if c.get("ref"):
            leaf["ref"] = c["ref"]               # compare against price, or another band
        elif c["op"] not in BOOL_OPS:
            leaf["value"] = c.get("value")
        return leaf
    if kind == "session":
        return {"type": "session_in", "sessions": c.get("sessions") or []}
    if kind == "regime":
        return {"type": "adx_regime", "timeframe": c.get("timeframe") or "4h",
                "trending": bool(c.get("trending", True))}
    return None


def describe_leaf(c: dict) -> str:
    kind = c.get("kind")
    if kind == "session":
        return "it is " + (" or ".join(c.get("sessions") or []) or "any session")
    if kind == "regime":
        return ("the market is trending" if c.get("trending", True)
                else "the market is ranging")
    if kind != "indicator":
        return str(kind)
    what = _label(c.get("id"))
    field = c.get("field") or "value"
    if field not in ("value", "present"):
        what += " " + field.replace("_", " ")
    op = OP_WORDS.get(c.get("op"), c.get("op") or "")
    tf = c.get("timeframe") or _TF
    if c.get("op") in BOOL_OPS:
        return f"{what} {op} on {tf}"
    if c.get("ref") == "price":
        return f"{what} {op} the price on {tf}"
    v = c.get("value")
    if isinstance(v, (list, tuple)):
        v = " and ".join(str(x) for x in v)
    return f"{what} on {tf} {op} {v}"


# The presets are the same grammar with the values filled in — one click for the
# question that gets asked most, not a second code path.
PRESETS = {
    "rsi_below": lambda f: {"kind": "indicator", "id": "rsi", "field": "value",
                            "op": "lt", "value": f.get("value"),
                            "timeframe": f.get("timeframe") or _TF},
    "rsi_above": lambda f: {"kind": "indicator", "id": "rsi", "field": "value",
                            "op": "gt", "value": f.get("value"),
                            "timeframe": f.get("timeframe") or _TF},
    "only_trending": lambda f: {"kind": "regime", "trending": True},
    "only_ranging": lambda f: {"kind": "regime", "trending": False},
    "in_fvg": lambda f: {"kind": "indicator", "id": "fvg", "field": "present",
                         "op": "is_true", "timeframe": f.get("timeframe") or _TF},
    "at_order_block": lambda f: {"kind": "indicator", "id": "order_block",
                                 "field": "dist_pct", "op": "lte",
                                 "value": f.get("value", 0.5),
                                 "timeframe": f.get("timeframe") or _TF},
}


def preset_leaf(f: dict) -> Optional[dict]:
    """A named preset -> the same operator condition a hand-built one produces.

    `skip_session` is the one inversion: it is stated as a skip, so it becomes
    "only trade when it is NOT one of these"."""
    kind = f.get("kind")
    if kind == "skip_session":
        return {"kind": "not_session", "sessions": f.get("sessions") or []}
    fn = PRESETS.get(kind)
    return fn(f) if fn else None


def _leaf_of(c: dict) -> Optional[dict]:
    """Operator condition -> engine leaf, including the negated session case."""
    if c.get("kind") == "not_session":
        inner = {"type": "session_in", "sessions": c.get("sessions") or []}
        return {"not": inner}
    return keep_leaf(c)


def conditions_of(changes: dict) -> List[dict]:
    """Every "only trade when" condition the operator stated, presets and
    free-form together, in the order they authored them."""
    out = []
    for f in (changes.get("filters") or []):
        if f.get("kind") in GEOMETRY_KINDS:
            continue                             # applied on the signal set instead
        leaf = preset_leaf(f)
        if leaf:
            out.append(leaf)
    for c in (changes.get("conditions") or []):
        if keep_leaf(c) is not None or c.get("kind") == "not_session":
            out.append(c)
    return out


def entry_rules(changes: dict) -> List[dict]:
    """Every condition folded into ONE skip rule on the negation of all of them.

    `mode: live` so it actually skips in the simulation — this is a
    counterfactual, not a shadow."""
    leaves = [_leaf_of(c) for c in conditions_of(changes)]
    leaves = [x for x in leaves if x]
    if not leaves:
        return []
    return [{"enabled": True, "mode": "live", "action": "skip",
             "name": "does not meet the what-if entry conditions",
             "when": {"not": {"all": leaves}}}]


# --- exit: any ladder the engine can run --------------------------------------
# `strategy/rules.py` fires on three triggers and moves the stop to four targets.
# All of them are offered; nothing here extends the engine.
def trigger_of(t: dict) -> Optional[dict]:
    """One operator trigger -> one engine trigger, or None if it is unusable.

    A distance of ZERO is refused rather than passed through. `price_move` with
    0 points fires the moment price is not losing, so the ladder becomes an
    instant breakeven stop — measured on GOLD VIP: stop-outs went 27 -> 72 and
    the report blamed the exit the operator thought they had built. Same for
    `be_lock_at_r` at 0R."""
    kind = (t or {}).get("kind")
    if kind == "tp":
        return {"type": "tp_hit", "index": int(t.get("index") or 1)}
    if kind == "points":
        pts = float(t.get("points") or 0)
        return {"type": "price_move", "points": pts} if pts > 0 else None
    if kind == "r":
        r = float(t.get("r") or 0)
        return {"type": "be_lock_at_r", "r": r} if r > 0 else None
    return None


def action_of(a: dict) -> Optional[dict]:
    kind = (a or {}).get("kind")
    if kind == "breakeven":
        return {"type": "move_sl_to", "target": "entry"}
    if kind == "previous_tp":
        return {"type": "move_sl_to", "target": "previous_tp"}
    if kind == "tp":
        return {"type": "move_sl_to", "target": "tp", "index": int(a.get("index") or 1)}
    return None


def step_rule(step: dict) -> Optional[dict]:
    """One operator step -> one engine sl_rule, or None if it could never fire.

    `previous_tp` is resolved from the TRIGGER's index (`rules.py::_target_sl`),
    so pairing it with a price or R trigger yields a rule that evaluates to no
    target and silently does nothing. Refused here rather than shipped."""
    trig, act = trigger_of(step.get("when")), action_of(step.get("then"))
    if not trig or not act:
        return None
    if act.get("target") == "previous_tp" and trig["type"] != "tp_hit":
        return None
    return {"trigger": trig, "action": act}


def describe_step(step: dict) -> str:
    t, a = step.get("when") or {}, step.get("then") or {}
    when = ("TP" + str(t.get("index") or 1) + " is hit" if t.get("kind") == "tp"
            else f"price moves {t.get('points')} in our favour" if t.get("kind") == "points"
            else f"profit reaches {t.get('r')}R" if t.get("kind") == "r"
            else "?")
    then = ("breakeven" if a.get("kind") == "breakeven"
            else "the previous target" if a.get("kind") == "previous_tp"
            else "TP" + str(a.get("index") or 1) if a.get("kind") == "tp"
            else "?")
    return f"when {when}, move the stop to {then}"


def exit_ladder(changes: dict) -> Optional[List[dict]]:
    """The what-if arm's `sl_rules`, or None to leave the live ones alone.

    An EMPTY list would read as UNSET and cascade to the DEFAULT ladder, so
    "never move the stop" cannot be expressed as `[]` — it is a rule whose
    trigger can never fire."""
    steps = changes.get("exit_steps")
    if isinstance(steps, list) and steps:
        rules = [r for r in (step_rule(s) for s in steps) if r]
        return rules or None
    return EXITS.get(changes.get("exit"))


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
    tested — and with a free-form builder in front of it, that is the only thing
    standing between the operator and a column headed with a JSON blob."""
    bits = []
    conds = conditions_of(changes)
    if conds:
        parts = [("it is not " + (" or ".join(c.get("sessions") or []))
                  if c.get("kind") == "not_session" else describe_leaf(c))
                 for c in conds]
        bits.append("only trade when " + " and ".join(parts))
    for f in (changes.get("filters") or []):
        if f.get("kind") in GEOMETRY_KINDS:
            bits.append(f"skip signals whose stop is under {f.get('value')}x ATR")
    steps = changes.get("exit_steps")
    if isinstance(steps, list) and steps:
        bits.append(" then ".join(describe_step(s) for s in steps))
    elif changes.get("exit") in EXIT_LABELS:
        bits.append(EXIT_LABELS[changes["exit"]])
    if changes.get("risk_percent"):
        bits.append(f"risk {changes['risk_percent']}% per trade")
    if changes.get("entry_style"):
        bits.append(f"{changes['entry_style']} entries")
    return " + ".join(bits) if bits else "no change"


def apply_changes(variant: dict, changes: dict) -> dict:
    """A live variant + the operator's change = the what-if arm."""
    v = copy.deepcopy(variant)
    rules = entry_rules(changes)
    exit_rules = exit_ladder(changes)
    for st in v.get("strategies", []):
        base = st.get("account_id") is None and st.get("source_id") is None
        if rules:
            # Replace rather than append: the operator is asking "what if we
            # filtered by THIS", not "what if we added it on top of whatever is
            # already there and could not see".
            #
            # The scoped layer is EMPTIED, not set to `{"rules": []}`.
            # `resolve_entry_filters` returns the first TRUTHY block walking
            # most-specific first, and `{"rules": []}` is truthy — so the
            # placeholder won the cascade and the arm ran with no filtration at
            # all. Measured: a FVG + order-block condition that skips 40 of GOLD
            # VIP's 120 signals in isolation skipped 0 in the run, and the report
            # attributed the difference to a filter that never fired.
            if base:
                st.setdefault("entry_filters", {})["rules"] = copy.deepcopy(rules)
            else:
                st["entry_filters"] = {}          # falsy -> the cascade continues
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
BARELY = 0.05          # a filter touching under 5% has not been tested
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
#
# The detector itself now lives in `provenance.py` (#192) so the loader that
# EXCLUDES backfilled history and the caveat that DECLARES an unmarked burst
# agree on what a burst is. This page keeps declaring: once the known rows carry
# `backfilled`, `load_signals` drops them and this text stops firing for them —
# but it still fires for the next onboarding, which nobody has marked yet.
BULK_BAR_MINUTES = P.BULK_BAR_MINUTES
BULK_MIN_SIGNALS = P.BULK_MIN_SIGNALS
_bar_key = P.bar_key
_bursts = P.bursts


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


# A ratchet at TP N only protects something if a leg is still open AFTER TP N
# closes — i.e. the signal posted more than N targets.
EXIT_TRIGGER = {"be_at_tp1": 1, "be_at_tp2": 2}


def _needs_ladder_depth(changes: dict) -> Optional[int]:
    """The shallowest ladder depth this exit needs to do anything, or None when
    the question does not arise.

    None for a price or R trigger — those fire on excursion and do not care how
    many targets the channel posted — and None for `let_it_run`, whose trigger is
    unreachable ON PURPOSE. Anything else reports the smallest TP index it
    ratchets on, because that is the first step that could fire."""
    steps = changes.get("exit_steps")
    if isinstance(steps, list) and steps:
        idxs = []
        for s in steps:
            t = s.get("when") or {}
            if t.get("kind") != "tp":
                return None          # something in the ladder fires without TPs
            idxs.append(int(t.get("index") or 1))
        return min(idxs) if idxs else None
    return EXIT_TRIGGER.get(changes.get("exit"))


def exit_reach_caveat(signals, changes: dict) -> Optional[str]:
    """Say when the chosen exit could not fire on the signals it was tested on.

    MEASURED: all 114 Quartz Elite signals post exactly 2 targets, so
    `be_at_tp2` and `let_it_run` returned byte-identical results — TP2 closes
    the last leg and there is nothing left to move a stop on. The run was really
    measuring "stop ratcheting at TP1", and the verdict named the wrong cause.

    A change that cannot fire is the silent-no-op failure this module exists to
    refuse: nothing errors, the numbers move for a different reason, and the
    operator acts on an attribution that is wrong."""
    idx = _needs_ladder_depth(changes)
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
    steps = changes.get("exit_steps")
    label = (" then ".join(describe_step(s) for s in steps)
             if isinstance(steps, list) and steps
             else EXIT_LABELS.get(changes.get("exit"), changes.get("exit")))
    every = ("Every one of these" if unreachable == len(depths)
             else f"{unreachable} of these")
    target = "target" if idx == 1 else "targets"
    return (f"{every} {len(depths)} signals posts {idx} {target} at most, so "
            f"\"{label}\" never has a leg left to protect. Any difference you "
            "see below comes from REMOVING the exit you run today, not from "
            "adding this one.")


def reality_caveat(actual: Optional[dict], base: dict) -> Optional[str]:
    """Say it out loud when the simulated baseline and the real book disagree.

    MEASURED: GOLD VIP SIGNAL TM simulates at +1,438 over 2026-07-05..07-31
    while the account really lost 41,639 on it in the same window. The number
    is not wrong for what it is — today's config over this history — but read
    without the real one it turns the worst channel on the book into a winner.

    The dominant cause here is that `load_live_config` returns TODAY's config:
    the book ran 5% risk until 2026-07-25 and 2% since, so the whole window is
    simulated at the lower size. Filters and account mappings have moved too."""
    if not actual or not actual.get("trades"):
        return None
    real, sim = float(actual["profit"]), float(base.get("profit") or 0.0)
    # STRICTLY opposite signs. A simulated zero is not "the other direction",
    # it is no result — `far` is what catches that.
    flipped = (real < 0 < sim) or (sim < 0 < real)
    # Relative to the REAL magnitude: losing 41,639 and simulating a 1,100 loss
    # is the same misreading wearing a minus sign.
    far = abs(real - sim) > max(500.0, 0.5 * abs(real))
    if not (flipped or far):
        return None
    lead = ("The simulated column says you MADE money on a window where the "
            "account lost it. " if flipped and sim > 0 else
            "The simulated column says you LOST money on a window where the "
            "account made it. " if flipped else
            "The simulated column is a long way from what the account did. ")
    return (lead + f"Really traded: {real:,.2f} over {actual['trades']} trades. "
            f"Simulated here: {sim:,.2f}. This runs your CURRENT settings over "
            "old signals — risk sizing, filters and account routing have all "
            "changed since — so read the two columns against each other, not "
            "against your statement.")


def verdict(base: dict, alt: dict, changes: dict) -> dict:
    """One sentence a person can act on, plus the two numbers behind it."""
    delta = round(alt["profit"] - base["profit"], 2)
    removed = alt["skipped_by_rule"] - base["skipped_by_rule"]
    stated = bool(conditions_of(changes) or changes.get("filters"))
    lost_winners = max(0, base["wins"] - alt["wins"])
    change = describe(changes)

    if delta > 0:
        head = f"Better by {delta:,.2f}."
    elif delta < 0:
        head = f"Worse by {abs(delta):,.2f}."
    else:
        head = "No difference."

    parts = [head]
    # STATED AS NET COUNTS, not as attribution. The what-if REPLACES the filters
    # you run today rather than adding to them, so the two arms turn away
    # different SETS of signals — measured on GOLD VIP: today's filters skip 31,
    # the what-if's conditions skip 39, and the net of 8 described neither. The
    # old wording ("it skipped 8 signals you took before, 22 of them winners")
    # invented an attribution the numbers cannot support.
    if alt["executed"] != base["executed"]:
        parts.append(f"It traded {alt['executed']} signals instead of "
                     f"{base['executed']}.")
    gained_w = alt["wins"] - base["wins"]
    gained_l = alt["losses"] - base["losses"]
    if gained_w or gained_l:
        parts.append(
            f"Wins go {base['wins']} to {alt['wins']}, "
            f"losses {base['losses']} to {alt['losses']}.")
    if base["executed"] and alt["executed"] == 0:
        parts.append("It skipped EVERYTHING — the filter is too strict to test.")
    elif stated and not alt["skipped_by_rule"]:
        # THE CHECK THAT WOULD HAVE CAUGHT THE CASCADE BUG. Conditions were
        # stated and not one signal was turned away, so whatever moved the
        # numbers, it was not the filter. Either the conditions hold on every
        # signal, or their inputs could not be read — and the difference matters
        # far more than the delta printed above it.
        parts.append("Your conditions turned away NOTHING — every signal still "
                     "passed, so the difference below is not the filter. Check "
                     "the indicator and timeframe are ones we can read at these "
                     "signals' times.")
    elif stated and 0 < removed <= BARELY * max(1, base["executed"]):
        # Measured: an RSI-below-70 filter touched 2 of 114 Quartz signals,
        # because RSI is rarely that high when these channels post. The delta
        # was +80 on a -1,189 book, and reading that as "the filter helps" is
        # reading noise. Say the filter barely applied instead.
        #
        # A PROPORTION, not "removed nothing" — that case is the branch above,
        # and it means something different.
        parts.append(f"The filter barely applied — it turned away {removed} of "
                     f"{base['executed']}, so this says little either way. Try a "
                     "tighter threshold.")
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
           frm=None, to=None, signals=(), actual=None) -> dict:
    """The whole answer: two columns, a verdict, and what it cannot tell you."""
    # NOT "what happened". The left column is a SIMULATION of the current setup
    # over these signals, and on this book 179 of 856 signals were never traded
    # live at all. Calling it "what happened" put a claim on it that the number
    # cannot support — the Reconciler exists precisely because sim and broker
    # truth differ (agreement 0.9149, #187).
    base = summarise(base_res, label="Your setup now")
    alt = summarise(alt_res, label="What-if: " + describe(changes))
    caveats = bulk_ingest_caveats(signals, changes)
    # REPLACES, does not add. Measured on GOLD VIP: the live config already turns
    # away 31 signals, and the what-if arm turns away a DIFFERENT 39 — so the two
    # columns are not "before and after adding a filter", and a reader who
    # assumes they are will misread every row.
    if conditions_of(changes) and base["skipped_by_rule"]:
        caveats.append(
            f"Your conditions REPLACE the entry filters you run today, they do "
            f"not stack on them. Today's filters turn away "
            f"{base['skipped_by_rule']} of these signals; the what-if column "
            f"turns away {alt['skipped_by_rule']} — a different set, not a "
            "bigger one.")
    reach = exit_reach_caveat(signals, changes)
    if reach:
        caveats.insert(0, reach)      # it changes what the numbers MEAN
    real = reality_caveat(actual, base)
    if real:
        caveats.insert(0, real)       # and this one outranks even that
    return {
        "scope": scope_label,
        "from": frm.isoformat() if frm is not None else None,
        "to": to.isoformat() if to is not None else None,
        "change": describe(changes),
        "baseline": base,
        "whatif": alt,
        "verdict": verdict(base, alt, changes),
        "actual": actual,
        "caveats": caveats,
        "note": ("Same signals, run twice. This is a screening question — it says "
                 "whether a change is worth a real test, not whether to make it. "
                 "A live frozen-week A/B is still what promotes a config."),
    }
