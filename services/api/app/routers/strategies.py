"""Execution strategies (#84) — the config surface for per-(account, source)
Entry / Filtration / Exit policy. CRUD over `execution_strategies`.

Scope is (account_id, source_id), both nullable: null = "any". Most-specific
enabled scope wins at resolution time. The executor snapshots the resolved exit
rules onto each trade at entry, so edits here only affect FUTURE trades — running
A/B arms stay frozen (clean attribution). Risk is NOT here (Risk & Limits owns it)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis import epochs as EP
from beacon_core.analysis import provenance as PV
from beacon_core.db.models import Account, Event, ExecutionStrategy, Source
from beacon_core.execution import strategy as ST
from beacon_core.execution import staging as STG
from beacon_core.execution.strategy import ENTRY_POLICY_KEYS
from beacon_core.ta import registry as TA
from beacon_core.timeutil import utcnow
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/strategies", tags=["strategies"],
                   dependencies=[Depends(require_token)])

_SL_TARGETS = {"entry", "previous_tp", "tp", "number"}
_SL_TRIGGERS = {"tp_hit", "price_move", "be_lock_at_r"}   # be_lock_at_r: #109
# Extensible; see execution/strategy.apply_filter_rules. MUST stay in step with
# EntryFilterRules.jsx RULE_TYPES — a type the UI offers but this set omits is a
# 422 on save (mc_probability / turtle_signal shipped in #163 and were missing).
# `trend_alignment` is deliberately NOT here: Strategies.jsx converts that rule
# into the legacy entry_filters.trend_alignment block before saving, and the
# evaluator has no case for it, so accepting one into `rules` would store a
# permanently silent no-op.
_FILTER_WHEN = {"always", "session_in", "time_window", "adx_regime",
                "mc_probability", "turtle_signal", "indicator"}
_FILTER_ACTIONS = {"skip", "scale"}


def _valid_sl_rules(rules) -> bool:
    if rules is None:
        return True
    if not isinstance(rules, list):
        return False
    for r in rules:
        if not isinstance(r, dict):
            return False
        t, a = r.get("trigger"), r.get("action")
        if not isinstance(t, dict) or t.get("type") not in _SL_TRIGGERS:
            return False
        if not isinstance(a, dict) or a.get("type") != "move_sl_to" or a.get("target") not in _SL_TARGETS:
            return False
    return True


def _clean_entry_policy(ep) -> dict | None:
    """Keep only known entry-policy keys (#67 chase guard + TTL), validating the
    #129 staged-entry keys: entry_style (enum) and staged (nested block)."""
    if ep is None:
        return None
    if not isinstance(ep, dict):
        raise HTTPException(422, "entry_policy must be an object")
    out = {k: ep[k] for k in ENTRY_POLICY_KEYS
           if k in ep and ep[k] is not None and k not in ("entry_style", "staged")}
    if ep.get("entry_style") is not None:
        try:
            out["entry_style"] = STG.clean_entry_style(ep["entry_style"])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    if ep.get("staged") is not None:
        try:
            staged = STG.clean_staged_config(ep["staged"])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if staged:
            out["staged"] = staged
    return out or None


def _clean_when(when: dict) -> dict:
    """Drop cleared UI fields from a rule's `when` block (#164).

    `EntryFilterRules.jsx` stores an untouched optional numeric as `""` (the ADX
    Regime blank ships `min_adx: ""`). That is not None, so it slipped past every
    guard and reached `float("")` in the evaluator, on the executor's live entry
    path. Stripping here means the DB never holds one whichever client wrote it;
    `execution.strategy._as_num` remains the runtime backstop for rows already
    stored."""
    return {k: v for k, v in when.items() if v != ""}


def _check_time_window(when: dict, where: str) -> None:
    """Validate a `time_window` leaf (#214). Strict where the evaluator is
    fail-open: a window the evaluator cannot read is a permanently silent rule,
    and an operator who wrote `7:00` or `Europe/Lundon` deserves to hear about it
    at write time rather than a week later in the removed-set count."""
    for key in ("from", "to"):
        if ST._as_minute_of_day(when.get(key)) is None:
            raise HTTPException(422, f"{where}: time_window needs '{key}' as "
                                     f"HH:MM (got {when.get(key)!r})")
    if ST._as_minute_of_day(when["from"]) == ST._as_minute_of_day(when["to"]):
        raise HTTPException(422, f"{where}: time_window 'from' and 'to' are the "
                                 "same minute — that window is empty")
    days = when.get("days")
    if days not in (None, "", []):
        if not isinstance(days, (list, tuple)):
            raise HTTPException(422, f"{where}: time_window 'days' must be a list")
        for d in days:
            if str(d).strip().lower()[:3] not in ST._WEEKDAYS:
                raise HTTPException(422, f"{where}: unknown day '{d}' "
                                         f"(use {', '.join(ST._WEEKDAYS)})")
    tz = when.get("tz")
    if tz not in (None, "") and str(tz).strip().upper() not in ("UTC", "Z", "GMT"):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(tz).strip())
        except Exception:
            raise HTTPException(422, f"{where}: unknown timezone '{tz}'")


def _check_indicator_side(side: dict, where: str, *, need_op: bool) -> None:
    """Validate one side of a generic `indicator` rule (#167) against the registry.

    Deliberately STRICT where the evaluator is deliberately fail-open. The
    evaluator must never delete a signal by raising, so it reads a bad reference
    as "does not match" — which means a typo'd id or field would save fine and sit
    there as a permanently silent no-op. This is the layer that can afford to say
    no, so it does: unknown indicator, unknown field, unknown timeframe, unknown
    operator are all 422s at write time."""
    inst = TA.resolve_instance(side.get("id"), side.get("params"))
    if not inst:
        known = ", ".join(sorted(s["id"] for s in TA.REGISTRY))
        raise HTTPException(422, f"{where}: unknown indicator '{side.get('id')}' "
                                 f"(known: {known})")
    tf = side.get("timeframe")
    if tf not in (None, "") and tf not in TA.AVAILABLE_TIMEFRAMES:
        raise HTTPException(422, f"{where}: unknown timeframe '{tf}'")
    field = side.get("field") or "value"
    if field not in inst["outputs"]:
        raise HTTPException(422, f"{where}: '{inst['id']}' has no field '{field}' "
                                 f"(fields: {', '.join(inst['outputs'])})")
    if not need_op:
        return
    op = str(side.get("op") or "").strip().lower()
    if op not in ST.FILTER_OPS:
        raise HTTPException(422, f"{where}: unknown op '{side.get('op')}' "
                                 f"(ops: {', '.join(sorted(ST.FILTER_OPS))})")
    if op in ("between", "outside"):
        v = side.get("value")
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise HTTPException(422, f"{where}: op '{op}' needs value [lo, hi]")


def _check_indicator_when(when: dict, where: str) -> None:
    _check_indicator_side(when, where, need_op=True)
    ref = when.get("ref")
    if ref in (None, ""):
        return
    if isinstance(ref, str):
        if ref.strip().lower() != "price":
            raise HTTPException(422, f"{where}: ref must be 'price' or "
                                     "{id, timeframe, field}")
    elif isinstance(ref, dict):
        _check_indicator_side(ref, f"{where}.ref", need_op=False)
    else:
        raise HTTPException(422, f"{where}: ref must be 'price' or "
                                 "{id, timeframe, field}")


def _clean_mode(rule: dict, where: str) -> str | None:
    """Validate an explicit `mode`. Absent stays absent so the evaluator applies
    its type-dependent default (shadow for `indicator`, live for the rule types
    that predate it and are already deployed)."""
    m = rule.get("mode")
    if m in (None, ""):
        return None
    m = str(m).strip().lower()
    if m not in ST.FILTER_MODES:
        raise HTTPException(422, f"{where}: mode must be one of "
                                 f"{', '.join(ST.FILTER_MODES)}")
    return m


def _clean_entry_filters(ef) -> dict | None:
    if ef is None:
        return None
    if not isinstance(ef, dict):
        raise HTTPException(422, "entry_filters must be an object")
    rules = ef.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise HTTPException(422, "entry_filters.rules must be a list")
        cleaned = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict) or (r.get("when") or {}).get("type") not in _FILTER_WHEN \
                    or r.get("action") not in _FILTER_ACTIONS:
                raise HTTPException(422, "each filter rule needs a known when.type and action")
            where = f"rules[{i}]"
            when = _clean_when(r["when"])
            if when.get("type") == "indicator":
                _check_indicator_when(when, where)
            elif when.get("type") == "time_window":
                _check_time_window(when, where)
            out = {**r, "when": when}
            mode = _clean_mode(r, where)
            if mode:
                out["mode"] = mode
            else:
                out.pop("mode", None)
            # #201: a provenance block is a CLAIM about where a rule came from.
            # Validated strictly here — the opposite of the evaluator's
            # fail-open reading — because a malformed claim that is silently
            # kept reads as "recorded" to every later reviewer.
            if "provenance" in out:
                try:
                    p = PV.clean_provenance(out["provenance"])
                except ValueError as exc:
                    raise HTTPException(422, f"{where}: {exc}")
                if p is None:
                    out.pop("provenance", None)
                else:
                    out["provenance"] = p
            cleaned.append(out)
        ef = {**ef, "rules": cleaned}
    return ef or None


def _clean_exit_policy(xp) -> dict | None:
    if xp is None:
        return None
    if not isinstance(xp, dict):
        raise HTTPException(422, "exit_policy must be an object")
    if not _valid_sl_rules(xp.get("sl_rules")):
        raise HTTPException(422, "exit_policy.sl_rules must be a list of {trigger, action:move_sl_to}")
    return xp or None


def _armed(rule) -> bool:
    """Can this rule actually remove or resize a trade? Shadow rules are being
    MEASURED, which is the thing we want to stay cheap."""
    return bool(isinstance(rule, dict) and rule.get("enabled", True)
                and ST.rule_mode(rule) == "live")


def _rule_key(rule) -> str:
    """Identity of a rule for "is this the same one that was already running?".

    Reuses the epoch digest's canonicalisation, so `30` and `"30"` are one rule
    here for exactly the reason they are one epoch there."""
    return EP.epoch_digest({"rules": [rule]}, None)


def _check_promotions(new_rules, old_rules) -> list:
    """Gate the screen->live step (#201), and ONLY that step.

    Applied to rules being armed that were NOT already armed in this exact form.
    Re-saving a config that is already running cannot make anything worse, and
    refusing it would trap the operator behind a rule they did not write today —
    the six live `bt_` rules predate provenance entirely. What the gate stops is
    a NEW mined rule going live with nothing recorded about the run, the
    candidate count, or what it did out of sample.

    Returns the warnings; raises 422 on a refusal."""
    already = {_rule_key(r) for r in (old_rules or []) if _armed(r)}
    warnings = []
    for i, r in enumerate(new_rules or []):
        armed = _armed(r)
        warnings.extend(PV.promotion_warnings(r, armed=armed))
        if not armed or _rule_key(r) in already:
            continue
        verdict = PV.promotion_check(r, armed=True)
        if not verdict["ok"]:
            raise HTTPException(422, f"rules[{i}]: {verdict['reason']}")
    return warnings


async def _skips_since(db: AsyncSession, account_id: int | None, since) -> int:
    """Distinct signals this account skipped by filtration since `since`.

    Deduped by signal, not counted by event: one signal fans out to several legs
    and each can log its own `entry_filtered`, so counting rows would inflate the
    accumulation straight past the threshold it is measured against (the same
    trap `filter_removed_set` documents). This is the SKIP count, which bounds
    the decisive count from above — decisive removals are the subset the control
    actually scored, and only the weekly instrument can compute those because
    only it knows which account is the control."""
    if account_id is None or since is None:
        return 0
    rows = (await db.execute(
        select(Event.payload).where(Event.kind == "entry_filtered",
                                    Event.ts >= since))).scalars().all()
    sigs = set()
    for p in rows:
        if not isinstance(p, dict) or p.get("reason") != "filtration_skip":
            continue
        if p.get("account_id") != account_id:
            continue
        if p.get("signal_id") is not None:
            sigs.add(p["signal_id"])
    return len(sigs)


def _shape(s: ExecutionStrategy) -> dict:
    # `filter_modes` is derived, not stored (#167): how many enabled rules can
    # actually skip/scale a trade vs how many are only being measured. Every
    # simultaneous LIVE gate is another multiple comparison, so that count belongs
    # on the surface rather than buried in the rules blob.
    return {"id": s.id, "account_id": s.account_id, "source_id": s.source_id,
            "entry_policy": s.entry_policy, "entry_filters": s.entry_filters,
            "filter_modes": ST.filter_mode_counts((s.entry_filters or {}).get("rules")),
            "exit_policy": s.exit_policy, "enabled": s.enabled, "label": s.label,
            "note": s.note, "version": s.version,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            # #200: the epoch is what the removed set accumulates within, so it
            # belongs on the same surface as the rules rather than being
            # reconstructed from `updated_at` by whoever writes next week's script.
            "epoch_digest": s.epoch_digest,
            "epoch_started_at": (s.epoch_started_at.isoformat()
                                 if s.epoch_started_at else None),
            "epoch": EP.epoch_name(s.entry_filters, s.entry_policy,
                                   digest=s.epoch_digest),
            # #201: "backtest said -0.4R, holdout said -0.1R" as ONE line, not
            # three archaeology sessions. Keyed by rule name so the page can put
            # it on the rule it belongs to.
            "rule_provenance": {
                (r.get("name") or f"rules[{i}]"): {
                    "mined": PV.is_mined(r),
                    "status": (r.get("provenance") or {}).get("status"),
                    "armed": _armed(r),
                    **(PV.shrinkage(r) or {}),
                }
                for i, r in enumerate((s.entry_filters or {}).get("rules") or [])
                if isinstance(r, dict) and PV.is_mined(r)}}


@router.get("")
async def list_strategies(account_id: int | None = None, source_id: int | None = None,
                          db: AsyncSession = Depends(get_db)):
    """All strategies, optionally filtered to an exact account and/or source scope."""
    q = select(ExecutionStrategy)
    if account_id is not None:
        q = q.where(ExecutionStrategy.account_id == account_id)
    if source_id is not None:
        q = q.where(ExecutionStrategy.source_id == source_id)
    rows = (await db.execute(q.order_by(ExecutionStrategy.account_id.nulls_last(),
                                        ExecutionStrategy.source_id.nulls_last()))).scalars().all()
    return [_shape(s) for s in rows]


@router.get("/resolve")
async def resolve(account_id: int, source_id: int, db: AsyncSession = Depends(get_db)):
    """Preview which strategy (and pillars) a trade on (account, source) would run
    under — the most-specific enabled match, or null if none (global defaults apply)."""
    rows = (await db.execute(select(ExecutionStrategy))).scalars().all()
    s = ST.resolve_strategy(rows, account_id, source_id)
    return {"resolved": _shape(s) if s else None}


@router.put("")
async def upsert(body: dict, db: AsyncSession = Depends(get_db)):
    """Create/update the strategy for one scope. account_id/source_id may be null
    (= any). Bumps `version` on every edit for trade attribution."""
    def _scope(key):
        v = body.get(key)
        if v in (None, "", "any"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{key} must be an integer or null")
    account_id, source_id = _scope("account_id"), _scope("source_id")
    if account_id is not None and not await db.get(Account, account_id):
        raise HTTPException(404, f"account {account_id} not found")
    if source_id is not None and not await db.get(Source, source_id):
        raise HTTPException(404, f"source {source_id} not found")

    entry_policy = _clean_entry_policy(body.get("entry_policy"))
    entry_filters = _clean_entry_filters(body.get("entry_filters"))
    exit_policy = _clean_exit_policy(body.get("exit_policy"))

    existing = (await db.execute(select(ExecutionStrategy).where(
        ExecutionStrategy.account_id.is_(account_id) if account_id is None
        else ExecutionStrategy.account_id == account_id,
        ExecutionStrategy.source_id.is_(source_id) if source_id is None
        else ExecutionStrategy.source_id == source_id))).scalar_one_or_none()
    # #201: the screen->live gate, against what is ALREADY armed on this scope.
    promo = _check_promotions((entry_filters or {}).get("rules"),
                              ((existing.entry_filters or {}).get("rules")
                               if existing else None))

    now = utcnow()
    new_digest = EP.epoch_digest(entry_filters, entry_policy)
    warning = None
    if existing:
        # #200: the epoch moves on a SEMANTIC change and only then. A relabel
        # bumps `version` and `updated_at` exactly as a rule change does, which
        # is why neither could ever say whether an accumulation had been thrown
        # away — and why three consecutive Arm-B verdicts died mid-accumulation
        # with nothing recording the cost.
        old_digest = existing.epoch_digest
        old_started = existing.epoch_started_at or existing.updated_at
        move = EP.epoch_transition(old_digest, new_digest, old_started, now)
        if move["closed"]:
            n_skips = await _skips_since(db, account_id, old_started)
            old_name = EP.epoch_name(existing.entry_filters,
                                     existing.entry_policy, digest=old_digest)
            warning = {
                "epoch_closed": old_name, "epoch_started_at":
                    old_started.isoformat() if old_started else None,
                "n_skips_accumulated": n_skips,
                "note": EP.epoch_close_note(old_name=old_name,
                                            started_at=old_started,
                                            n_skips=n_skips),
                "decisive_count": ("computed by filter_removed_set against the "
                                   "control arm; n_skips is its upper bound"),
            }
            # An `events` row as well as the response: the operator who makes
            # this call is not necessarily the one who reads next week's report,
            # and a warning that exists only in an HTTP response is a warning
            # nobody can audit afterwards.
            db.add(Event(kind="epoch_closed", payload={
                "account_id": account_id, "source_id": source_id,
                "strategy_id": existing.id, **warning}))
        existing.epoch_digest = move["digest"]
        existing.epoch_started_at = move["started_at"]
        existing.entry_policy = entry_policy
        existing.entry_filters = entry_filters
        existing.exit_policy = exit_policy
        existing.enabled = bool(body.get("enabled", True))
        existing.label = (body.get("label") or None)
        existing.note = (body.get("note") or None)
        existing.version = (existing.version or 1) + 1
        existing.updated_at = now
        row = existing
    else:
        row = ExecutionStrategy(
            account_id=account_id, source_id=source_id, entry_policy=entry_policy,
            entry_filters=entry_filters, exit_policy=exit_policy,
            enabled=bool(body.get("enabled", True)),
            label=(body.get("label") or None), note=(body.get("note") or None),
            epoch_digest=new_digest, epoch_started_at=now)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    out = _shape(row)
    if warning:
        out["epoch_warning"] = warning
    if promo:
        # Not a refusal — "how hard did we look before it worked?" is a
        # judgement about a research programme, not about one rule.
        out["provenance_warnings"] = promo
    return out


@router.delete("/{strategy_id}")
async def delete(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a strategy (future trades revert to the next-specific scope / default).
    Existing trades keep their exit snapshot, so their A/B arm is unaffected."""
    row = await db.get(ExecutionStrategy, strategy_id)
    if not row:
        raise HTTPException(404, "strategy not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": strategy_id}
