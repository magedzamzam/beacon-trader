"""Where a mined filter rule came from, and whether it earned its place (#201).

Six live entry filters are named `bt_1h_cci_value_gte100` and the like. The
`bt_` prefix and the embedded threshold were the ONLY record that they came out
of a backtest — nothing linked a live rule to the run that produced it, so no
reviewer could answer the one question that decides whether a mined rule is
signal or winner's curse: **how many candidates were screened to find it, and
what did it do out of sample?**

Roughly 900 configurations were screened over ~4 weeks of a single instrument.
Config-driven filtration (#167) plus the shared condition grammar (#184) mean a
new gate is now JSON rather than a deploy, so the cost of arming a rule has
collapsed and the discipline has to move from "can we express it?" to "how many
did we try before this one worked?".

Pure: shape validation, the promotion gate, and the shrinkage line. No DB, no
HTTP. Nothing here is consulted on the trading path — an armed rule that never
passed this gate still filters exactly as before; this decides what may be
WRITTEN, not what happens at signal time.
"""
from __future__ import annotations

from typing import Optional

STATUS_RECORDED = "recorded"
STATUS_UNRECORDED = "unrecorded"
STATUSES = (STATUS_RECORDED, STATUS_UNRECORDED)

# A held-out effect measured on a handful of trades is not a holdout, it is a
# rounding error with a date range attached. Deliberately low: the point is to
# refuse the UNMEASURED, not to adjudicate the underpowered — which the warning
# below does instead, loudly, without blocking.
MIN_HOLDOUT_N = 10

# Candidates screened per held-out trade. At 250 variants against 11 held-out
# trades the screen is ~23 candidates deep per observation, which is a setting
# where the best-looking rule is expected to look good by chance alone.
SELECTION_INTENSITY_WARN = 5.0

_EFFECT_KEYS = ("n", "mean_r", "net")
_NAME_PREFIX = "bt_"


def _effect(v, where: str) -> Optional[dict]:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError(f"{where} must be an object with n / mean_r / net")
    out = {}
    for k in _EFFECT_KEYS:
        if v.get(k) is None:
            continue
        try:
            out[k] = float(v[k])
        except (TypeError, ValueError):
            raise ValueError(f"{where}.{k} must be a number")
    if "n" in out:
        out["n"] = int(out["n"])
    return out or None


def clean_provenance(p) -> Optional[dict]:
    """Validate and normalise a rule's `provenance` block.

    STRICT on purpose, and the opposite of the evaluator's fail-open reading of
    a rule: a provenance block is a CLAIM about where a rule came from, and a
    malformed claim that is silently kept is worse than none at all — it reads
    as recorded to every later reviewer."""
    if p is None:
        return None
    if not isinstance(p, dict):
        raise ValueError("provenance must be an object")
    status = str(p.get("status") or STATUS_RECORDED).strip().lower()
    if status not in STATUSES:
        raise ValueError(f"provenance.status must be one of {STATUSES}")
    out: dict = {"status": status}
    if status == STATUS_UNRECORDED:
        # The honest marker for the rules that predate this: it says "nobody
        # knows", which a reviewer can act on. Anything else claimed alongside
        # it would be pretending to know half.
        if p.get("note"):
            out["note"] = str(p["note"])
        return out

    if p.get("replay_run_id") is not None:
        try:
            out["replay_run_id"] = int(p["replay_run_id"])
        except (TypeError, ValueError):
            raise ValueError("provenance.replay_run_id must be an integer")
    for k in ("variant", "variant_digest", "promoted_at", "note"):
        if p.get(k):
            out[k] = str(p[k])
    if p.get("n_candidates_screened") is not None:
        try:
            out["n_candidates_screened"] = int(p["n_candidates_screened"])
        except (TypeError, ValueError):
            raise ValueError("provenance.n_candidates_screened must be an integer")
    for k in ("train", "holdout"):
        w = p.get(k)
        if w is None:
            continue
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            raise ValueError(f"provenance.{k} must be [from, to]")
        out[k] = [str(w[0]), str(w[1])]
    for src, dst in (("effect_in_sample", "effect_in_sample"),
                     ("effect_holdout", "effect_holdout")):
        e = _effect(p.get(src), f"provenance.{src}")
        if e is not None:
            out[dst] = e
    return out


def is_mined(rule: dict) -> bool:
    """A rule the screen produced, rather than one an operator reasoned out.

    Two independent tells, either sufficient: the `bt_` naming convention the
    promotions already use, and the presence of a provenance block. Naming is a
    convention and conventions drift, which is why it is not the only test."""
    if not isinstance(rule, dict):
        return False
    if rule.get("provenance") is not None:
        return True
    return str(rule.get("name") or "").lower().startswith(_NAME_PREFIX)


def selection_intensity(prov: Optional[dict]) -> Optional[float]:
    """Candidates screened per held-out observation.

    Not a multiplicity correction and not offered as one — a ratio is enough to
    say "this rule beat 250 others on 11 trades", which is the sentence a
    reviewer needs."""
    prov = prov or {}
    n_cand = prov.get("n_candidates_screened")
    n_hold = (prov.get("effect_holdout") or {}).get("n")
    if not n_cand or not n_hold:
        return None
    return round(float(n_cand) / float(n_hold), 2)


def _sign(v) -> int:
    return 0 if v is None or v == 0 else (1 if v > 0 else -1)


def promotion_check(rule: dict, *, armed: bool) -> dict:
    """May this rule be ARMED? `{"ok": bool, "code": str, "reason": str}`.

    Only armed rules are gated. A shadow rule is being measured, which is the
    thing we WANT to be cheap — refusing to record a hypothesis would push the
    screening back out of the system, where nothing can see it.
    """
    if not armed or not is_mined(rule):
        return {"ok": True, "code": "not_gated", "reason": ""}
    name = rule.get("name") or "<unnamed>"
    prov = rule.get("provenance")
    if prov is None:
        return {"ok": False, "code": "no_provenance", "reason": (
            f"{name} looks mined but records nothing about the run that produced "
            "it. Add a provenance block, or mark it "
            '`{"status": "unrecorded"}` to say plainly that the link is lost.')}
    if prov.get("status") == STATUS_UNRECORDED:
        # Grandfathered, and visibly so. A reviewer reading this rule learns
        # that its origin is unknown, which is the whole point — it is an
        # answer, not a silence.
        return {"ok": True, "code": "unrecorded", "reason": (
            f"{name} is armed with its provenance explicitly unrecorded")}
    hold = prov.get("effect_holdout")
    if not hold or hold.get("n") is None:
        return {"ok": False, "code": "no_holdout", "reason": (
            f"{name} has no held-out effect. In-sample performance is what the "
            "screen selected ON, so it cannot also be the evidence for it.")}
    if int(hold["n"]) < MIN_HOLDOUT_N:
        return {"ok": False, "code": "holdout_too_small", "reason": (
            f"{name} was held out on {hold['n']} trade(s), below the "
            f"{MIN_HOLDOUT_N} floor — that is not a holdout, it is a rounding "
            "error with a date range attached.")}
    ins = prov.get("effect_in_sample") or {}
    s_in, s_out = _sign(ins.get("mean_r")), _sign(hold.get("mean_r"))
    if s_in and s_out and s_in != s_out:
        return {"ok": False, "code": "sign_flip", "reason": (
            f"{name}: in-sample mean R {ins.get('mean_r')} and held-out "
            f"{hold.get('mean_r')} have opposite signs. The effect did not "
            "survive the split, which is what a holdout is for.")}
    return {"ok": True, "code": "passed", "reason": ""}


def promotion_warnings(rule: dict, *, armed: bool) -> list:
    """Things a reviewer should be told but that must not block a write.

    Separate from the gate on purpose. The gate answers "is this measured?"; a
    warning answers "how hard did we look before it worked?", and that is a
    judgement about a whole research programme rather than about one rule."""
    if not armed or not is_mined(rule):
        return []
    prov = rule.get("provenance") or {}
    name = rule.get("name") or "<unnamed>"
    out = []
    if prov.get("status") == STATUS_UNRECORDED:
        out.append(f"{name}: provenance unrecorded — it cannot be reviewed, "
                   "only trusted.")
        return out
    ratio = selection_intensity(prov)
    if ratio is not None and ratio >= SELECTION_INTENSITY_WARN:
        out.append(
            f"{name}: {prov.get('n_candidates_screened')} candidates screened "
            f"against {(prov.get('effect_holdout') or {}).get('n')} held-out "
            f"trades ({ratio} per observation) — at that depth the best-looking "
            "rule is expected to look good by chance.")
    hold = (prov.get("effect_holdout") or {}).get("mean_r")
    ins = (prov.get("effect_in_sample") or {}).get("mean_r")
    if ins is not None and hold is not None and abs(hold) < abs(ins) * 0.5:
        out.append(f"{name}: held-out effect {hold} is less than half the "
                   f"in-sample {ins} — expected shrinkage, worth pricing in.")
    return out


def shrinkage(rule: dict, live: Optional[dict] = None) -> Optional[dict]:
    """in-sample -> holdout -> live, as one line rather than three archaeology
    sessions. Systematic decay across that sequence IS the winner's-curse
    measurement, and it is the number that should govern how aggressively mined
    rules get promoted in future weeks."""
    prov = (rule or {}).get("provenance") or {}
    if prov.get("status") == STATUS_UNRECORDED or not prov:
        return None
    ins = prov.get("effect_in_sample") or {}
    hold = prov.get("effect_holdout") or {}
    live = live or {}

    def _fmt(e):
        if not e or e.get("mean_r") is None:
            return "—"
        n = e.get("n")
        return f"{e['mean_r']:+.4f}" + (f" (n={n})" if n is not None else "")
    return {
        "in_sample": ins or None, "holdout": hold or None, "live": live or None,
        "line": (f"in-sample {_fmt(ins)} → holdout {_fmt(hold)} → "
                 f"live {_fmt(live)}"),
        "selection_intensity": selection_intensity(prov),
    }
