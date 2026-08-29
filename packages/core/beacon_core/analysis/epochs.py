"""Filter-rule EPOCHS, and the arm that stops trading (#200).

A live `entry_filters` rule is scored by its REMOVED SET, accumulated across
weeks and tested ONCE at N>=30 decisive removals (`report.filter_removed_set`,
#186). Any write that changes the rule starts a NEW epoch and resets that
accumulation to zero. Until this module existed nothing said so: `version` and
`updated_at` bump on a relabel exactly as they do on a semantic edit, so the
epoch boundary had to be reconstructed by hand from timestamps — which is how
Arm B's 52-removal accumulation was orphaned by a `min_adx` tweak with no
record that anything had been discarded.

Two pure pieces live here, both MEASUREMENT ONLY (CLAUDE.md §2 shadow-first —
nothing in this module is consulted on the trading path):

  * `epoch_digest` / `epoch_name` — a stable identity for a rule CONFIGURATION,
    so a cosmetic edit provably does not open an epoch and a semantic one
    provably does.
  * `dark_arm` — an arm skipping ~everything produces no trades and no
    information. That is a broken experiment, not a conservative one, and it
    went unnoticed for two days because nothing was watching the skip rate.
"""
from __future__ import annotations

import hashlib
import json

# Rule keys that name a rule rather than describe it. Changing one of these
# CANNOT change what the filter does, so it must not reset an accumulation: the
# whole point of the digest is that a relabel is free and a rule change is not.
# `provenance` (#201) is here for the same reason: it records where a rule CAME
# FROM, and documenting a rule that is already running must not be the act that
# throws away its accumulation. Backfilling provenance onto the six live `bt_`
# rules would otherwise close every one of their epochs at once.
COSMETIC_KEYS = ("name", "note", "label", "comment", "description", "provenance")


def _canon(v):
    """Value -> a form two equal configurations always share.

    `min_adx: 30`, `30.0` and `"30"` are the same filter — the evaluator coerces
    all three (`execution/strategy._as_num`) — so they must produce one digest,
    or a UI that round-trips a number as a string would silently orphan an
    epoch. Dict key order is not information either."""
    if isinstance(v, bool):                    # before int: bool IS an int
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            f = float(s)
        except ValueError:
            return v
        return int(f) if f.is_integer() else f
    if isinstance(v, dict):
        return {k: _canon(v[k]) for k in sorted(v)}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v


def _semantic_rule(rule: dict) -> dict:
    """One rule, stripped to what changes its behaviour.

    `mode` and `enabled` stay: a rule flipped shadow->live starts filtering, and
    that IS a new experiment even though the `when` block is untouched."""
    return {k: _canon(v) for k, v in rule.items() if k not in COSMETIC_KEYS}


def semantic_filters(entry_filters) -> dict:
    """`entry_filters`, stripped to what changes what the filter DOES.

    Shared by both digests below so the two can never disagree about what a
    cosmetic edit to a rule is."""
    ef = entry_filters or {}
    rules = ef.get("rules")
    out_ef = {k: _canon(v) for k, v in ef.items()
              if k != "rules" and k not in COSMETIC_KEYS}
    if isinstance(rules, list):
        out_ef["rules"] = [_semantic_rule(r) if isinstance(r, dict) else _canon(r)
                           for r in rules]
    elif rules is not None:
        out_ef["rules"] = _canon(rules)
    return out_ef


def semantic_config(entry_filters, entry_policy) -> dict:
    """The part of a strategy row an epoch is defined over.

    `entry_filters` and `entry_policy` both, because the removed set is a
    property of the arm as it ran: switching `entry_style` to staged changes
    what the kept signals do, so pooling across that boundary describes an arm
    that never existed either. `exit_policy` is deliberately NOT here — the
    removed set is scored on the CONTROL's outcomes, so the filter arm's own
    exit ladder cannot move it.

    This is the identity for anything measured on an arm's KEPT trades, and it
    is what `execution_strategies.epoch_digest` persists. It is NOT the identity
    for the removed set — see `removed_set_digest` (#271)."""
    ep = entry_policy or {}
    out_ep = {k: _canon(v) for k, v in ep.items() if k not in COSMETIC_KEYS}
    return {"entry_filters": semantic_filters(entry_filters),
            "entry_policy": out_ep}


def epoch_digest(entry_filters, entry_policy) -> str:
    """Stable 16-hex identity of a rule configuration.

    Stable across processes and releases: `sort_keys` plus the canonicalisation
    above, never Python's `hash()` (salted per process, so it would invent an
    epoch boundary on every restart)."""
    blob = json.dumps(semantic_config(entry_filters, entry_policy),
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def removed_set_digest(entry_filters) -> str:
    """Stable 16-hex identity for the REMOVED-SET experiment (#271).

    Computed over `entry_filters` ALONE. The invariant, stated so it is not
    re-argued: **a removed signal is never traded on this arm at all** — no
    entry is attempted, no `entry_style` is consulted, no `sl_distance` is
    applied — and its outcome is read entirely off the CONTROL's trade. So
    nothing outside `entry_filters` can move it. That is verbatim the argument
    `semantic_config` already uses to exclude `exit_policy`; it applies to
    `entry_policy` for the same reason, and the two digests exist because one
    identity was serving two experiments with different invariants.

    The cost of not splitting them, measured: the 2026-08-24 act added
    `entry_style: "staged"` to acct#7 while leaving the `adx_regime@1h` rule
    byte-identical. The digest moved, so an accumulation of 117 decisive
    removals — four times the `min_n` floor — became 20. Worse, acct#5 and
    acct#7 ran the SAME rule that week and produced two epochs of 20 instead of
    one of 40, with results identical to the decimal: the split carried no
    information at all.

    Same canonicalisation and the same `sha1`-over-sorted-JSON as
    `epoch_digest`, never Python's `hash()` (salted per process)."""
    blob = json.dumps(semantic_filters(entry_filters),
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def removed_set_epoch(entry_filters, digest: str | None = None) -> str:
    """The legible removed-set epoch key, derived exactly like `epoch_name`.

    Deliberately the same shape (`<rules>#<8 hex>`) so a reader recognises it,
    and deliberately a DIFFERENT suffix so the two vocabularies can never be
    mistaken for one another when both appear on the same event."""
    return epoch_name(entry_filters, None,
                      digest=digest or removed_set_digest(entry_filters))


def _describe_rule(rule: dict) -> str:
    when = rule.get("when") or {}
    if not isinstance(when, dict):
        return "rule"
    parts = [str(when.get("type") or "rule")]
    tf = when.get("timeframe")
    if tf:
        parts.append(f"@{tf}")
    for k in sorted(when):
        if k in ("type", "timeframe"):
            continue
        v = _canon(when[k])
        if isinstance(v, (list, tuple)):
            v = "-".join(str(x) for x in v)
        parts.append(f"+{k}{v}")
    if str(rule.get("mode") or "").lower() == "shadow":
        parts.append("~shadow")
    return "".join(parts)


def epoch_name(entry_filters, entry_policy=None, digest: str | None = None) -> str:
    """A legible epoch key DERIVED from the rules, never hand-written.

    The weekly analysis used to key its removed sets off literals typed into a
    script (`"adx_regime@1h+min_adx30"`), which is exactly how a missed bump
    mis-assigns a week of skips to the wrong epoch. The digest suffix is what
    makes it an identity rather than a description: two configurations that read
    alike but differ somewhere cannot collide."""
    digest = digest or epoch_digest(entry_filters, entry_policy)
    rules = ((entry_filters or {}).get("rules") or [])
    live = [r for r in rules
            if isinstance(r, dict) and r.get("enabled", True) is not False]
    body = "+".join(_describe_rule(r) for r in live) if live else "no_filter"
    return f"{body}#{digest[:8]}"


def event_stamp(entry_filters, entry_policy=None) -> dict:
    """The epoch keys a filtration EVENT must carry, decided at emit time (#253).

    #200 put the epoch on the strategy ROW, which is enough only if nobody ever
    edits history: the weekly still had to reconstruct which skips belonged to
    which configuration by comparing event timestamps against `updated_at`. When
    the digest was missing entirely, that reconstruction pooled 194 skips spanning
    THREE `adx_regime` configurations into one epoch and returned
    `REMOVES_LOSERS`; split correctly every one of the three is `NO_EVIDENCE`.

    An event that carries the epoch it was generated under cannot be reassigned
    to a filter that was not running, whatever the strategy row later becomes —
    so this is computed from the rules AS THEY RAN and never read back off the
    row. A stored digest that disagrees with its own pillars is stale (a write
    that bypassed the API), and trusting it here would stamp the skips of the new
    configuration with the identity of the old one, which is the bug itself.

    Returns FOUR keys, two pairs, because two experiments read this event and
    they do not share an invariant (#271):

      * `epoch_digest` / `epoch` — over `entry_filters` + `entry_policy`. The
        join to `execution_strategies`, and the identity for anything measured on
        this arm's KEPT trades. Unchanged, so nothing that reads them breaks.
      * `removed_epoch_digest` / `removed_epoch` — over `entry_filters` alone.
        What `report.filter_removed_set` groups on: the removed signal was never
        traded here, so nothing else can move its outcome.
    """
    digest = epoch_digest(entry_filters, entry_policy)
    rdigest = removed_set_digest(entry_filters)
    return {"epoch_digest": digest,
            "epoch": epoch_name(entry_filters, entry_policy, digest=digest),
            "removed_epoch_digest": rdigest,
            "removed_epoch": removed_set_epoch(entry_filters, digest=rdigest)}


def regroup_legacy_skips(skips, aliases) -> list:
    """Re-key pre-#271 skips onto a removed-set epoch, BY HAND and on the record.

    **There is no automatic backfill, and this says why rather than leaving a
    silent gap.** Recomputing `removed_epoch_digest` for an existing
    `entry_filtered` event would need the `entry_filters` that were live at that
    event's `ts`. `execution_strategies` keeps one CURRENT row per
    (account, source) — there is no history table and no audit trail of previous
    pillar values — so for any row written more than once the historical filters
    are simply gone. The combined `epoch` on the old events is not enough either:
    two skips sharing it definitely share `entry_filters`, but two with different
    values may differ only in `entry_policy`, which is exactly the case that has
    to be merged. So the merge is a judgement made from the strategy history the
    analyst has, and this makes it an explicit, reviewable argument instead of a
    literal retyped into a weekly script.

    `aliases` maps a legacy `epoch` value to the removed-set key it belongs to.
    Rows already carrying `removed_epoch` (post-fix) are returned untouched:
    the emitter's stamp is evidence and always outranks a hand mapping.

    Returns new dicts; the input rows are never mutated."""
    amap = dict(aliases or {})
    out = []
    for row in skips or ():
        r = dict(row)
        if not r.get("removed_epoch"):
            key = amap.get(str(r.get("epoch") or ""))
            if key:
                r["removed_epoch"] = key
        out.append(r)
    return out


# --- the arm that went dark ---------------------------------------------------
# 2026-08-05/06: Arm B took 1 trade then 0 while the control took 35 and 28. An
# arm skipping ~100% is not a conservative arm, it is an experiment that has
# stopped producing information — and the only reason it surfaced is that a
# human noticed a flat account, which then orphaned the epoch by hand-tightening
# the threshold mid-week.
DARK_MIN_SIGNALS = 10          # below this a high rate is noise, not a signal
DARK_SKIP_RATE = 0.80          # >= this share skipped over the window = dark


def dark_arm(n_signals: int, n_skipped: int, *,
             min_signals: int = DARK_MIN_SIGNALS,
             threshold: float = DARK_SKIP_RATE) -> dict:
    """Is this arm still running an experiment, over one rolling window?

    Both bounds are inclusive: exactly `min_signals` signals at exactly
    `threshold` IS dark. An alarm whose boundary is ambiguous gets argued with
    instead of acted on."""
    n_signals = int(n_signals or 0)
    n_skipped = int(n_skipped or 0)
    rate = (n_skipped / n_signals) if n_signals else None
    dark = bool(n_signals >= min_signals and rate is not None
                and rate >= threshold)
    if n_signals < min_signals:
        reason = (f"{n_signals} signals in the window, below the {min_signals} "
                  "needed before a skip rate means anything")
    elif dark:
        reason = (f"{n_skipped} of {n_signals} signals skipped "
                  f"({round(rate, 4)}) — at or above {threshold}, so the arm is "
                  "producing no trades and no information")
    else:
        reason = (f"{n_skipped} of {n_signals} signals skipped "
                  f"({round(rate, 4)}) — below {threshold}")
    return {"n_signals": n_signals, "n_skipped": n_skipped,
            "skip_rate": None if rate is None else round(rate, 4),
            "min_signals": min_signals, "threshold": threshold,
            "dark": dark, "reason": reason}


def epoch_transition(old_digest: str | None, new_digest: str,
                     old_started_at, now) -> dict:
    """What a write does to the epoch clock — the whole decision, in one place.

    THREE CASES, and the middle one is the reason this exists:

      * never stamped (`old_digest is None`) — a row that predates #200. ADOPT
        the current rules as the open epoch and keep the existing start: the
        accumulation in flight is real evidence and did not begin at deploy
        time. Stamping `now` here would reset every live epoch at once, which is
        precisely the bug.
      * digest unchanged — a relabel, a note, an `enabled` toggle on the row.
        The clock does NOT move. This is what `version`/`updated_at` could never
        express.
      * digest changed — a new experiment. The clock restarts and the closing
        epoch's accumulation is spent; `closed` says so, so the caller can
        report the cost before it lands.
    """
    if old_digest is None:
        return {"digest": new_digest, "started_at": old_started_at, "closed": False}
    if old_digest == new_digest:
        return {"digest": old_digest, "started_at": old_started_at, "closed": False}
    return {"digest": new_digest, "started_at": now, "closed": True}


def epoch_close_note(*, old_name: str | None, started_at, n_skips: int,
                     n_decisive=None, verdict=None,
                     min_n: int = 30) -> str:
    """The sentence a digest-changing write should print before it lands.

    Stated as a loss, not as a fact: an epoch cannot be reopened, so the cost of
    this write is exactly the evidence in flight, and the operator should see
    that number at the moment of the decision rather than in the next weekly
    report. It WARNS; it never blocks — there are good reasons to spend an
    accumulation, and none of them are the system's to judge."""
    since = getattr(started_at, "isoformat", lambda: str(started_at))()
    head = (f"this change closes epoch {old_name or '?'} (open since {since})")
    if n_decisive is None:
        return (f"{head} with {n_skips} accumulated skip(s). Its decisive count "
                f"and verdict are fixed at whatever the control scored — the "
                f"accumulation restarts at zero and cannot be reopened.")
    verdict = verdict or "?"
    short = "" if n_decisive >= min_n else f" — {min_n - n_decisive} short of a verdict"
    return (f"{head} at {n_decisive}/{min_n} decisive removals{short}. Its "
            f"verdict is frozen at {verdict} and cannot be reopened; the new "
            f"epoch accumulates from zero.")
