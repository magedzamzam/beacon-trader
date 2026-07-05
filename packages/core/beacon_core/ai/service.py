"""Orchestration glue: load AI config, run an assessment, persist the result.

Kept here (not in each worker) so telegram/executor/monitor/api all share one
code path. These helpers take an AsyncSession, tolerate the AI being disabled or
unreachable, and always return the stored AiAssessment or None.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AiAssessment, Signal, Source
from ..execution.planner import validate_signal as _geom_validate
from ..logging import get_logger
from ..parsing.models import ParsedSignal
from ..settings_store import get_setting
from . import assessments
from .config import AiConfig, resolve_ai_config
from .provider import AiUnavailable

log = get_logger("ai.service")

AI_SETTING_KEY = "ai"


async def load_config(session: AsyncSession) -> AiConfig:
    stored = await get_setting(session, AI_SETTING_KEY, {})
    return resolve_ai_config(stored)


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _signal_dict(sig: Signal, source: Optional[Source]) -> dict:
    return {
        "symbol": sig.symbol, "direction": sig.direction,
        "entry_from": str(sig.entry_from), "entry_to": str(sig.entry_to),
        "sl": str(sig.sl), "tps": sig.tps, "order_type": sig.order_type,
        "raw_text": sig.raw_text,
        "source_name": source.name if source else "unknown",
        "source_kind": source.kind if source else "unknown",
    }


async def _store(session: AsyncSession, *, kind: str, result: dict,
                 signal_id=None, trade_id=None, account_id=None) -> AiAssessment:
    row = AiAssessment(
        kind=kind, signal_id=signal_id, trade_id=trade_id, account_id=account_id,
        provider="anthropic", model=result.get("model"),
        verdict=result.get("verdict"),
        confidence=_dec(result.get("confidence")),
        score=_dec(result.get("score")),
        rationale=result.get("rationale"),
        payload=result.get("payload") or {},
    )
    session.add(row)
    await session.flush()
    return row


async def assess_signal(session: AsyncSession, sig: Signal,
                        source: Optional[Source], cfg: Optional[AiConfig] = None
                        ) -> Optional[AiAssessment]:
    cfg = cfg or await load_config(session)
    if not (cfg.ready and cfg.validate_signals):
        return None
    try:
        result = await assessments.validate_signal(cfg, _signal_dict(sig, source))
    except AiUnavailable as exc:
        log.info("signal %s: AI validation unavailable: %s", sig.id, exc)
        return None
    return await _store(session, kind="signal_validation", result=result,
                        signal_id=sig.id)


def _flag(sig: Signal, status: str, corrections=None) -> None:
    """Record the AI validation state on the signal itself (in market_snapshot,
    a JSON column) so the outcome is visible without a schema change."""
    snap = dict(sig.market_snapshot or {})
    snap["ai_validation"] = status
    if corrections is not None:
        snap["ai_corrections"] = corrections
    sig.market_snapshot = snap


def _corrected_parsed(c: dict, sig: Signal) -> Optional[ParsedSignal]:
    try:
        return ParsedSignal(
            symbol=str(c.get("symbol") or sig.symbol),
            direction=str(c.get("direction") or sig.direction).upper(),
            entry_from=Decimal(str(c.get("entry_from"))),
            entry_to=Decimal(str(c.get("entry_to"))),
            sl=Decimal(str(c.get("sl"))),
            tps=[Decimal(str(t)) for t in (c.get("tps") or [])],
            order_type_hint=str(c.get("order_type") or sig.order_type or "MARKET"),
            raw_text=sig.raw_text or "",
        )
    except (InvalidOperation, ValueError, TypeError):
        return None


async def apply_signal_validation(session: AsyncSession, sig: Signal,
                                  source: Optional[Source],
                                  cfg: Optional[AiConfig] = None) -> str:
    """Validate + correct a FREE-TEXT-parsed signal before it can trade.

    Mutates `sig` in place with the AI-corrected fields and records the outcome.
    Returns one of:
      * "validated"   — AI approved (maybe with corrections); sig updated; execute.
      * "rejected"    — AI says it isn't a valid signal / reject; do NOT execute.
      * "unvalidated" — AI unavailable or timed out; sig unchanged; execute anyway
                        (fail-open) but flagged so it's visible.
      * "skipped"     — AI disabled or validate_signals off; behave as before.

    The caller decides whether to publish based on the returned status.
    """
    cfg = cfg or await load_config(session)
    if not (cfg.ready and cfg.validate_signals):
        return "skipped"

    def _store_row(verdict, confidence, rationale, payload):
        return _store(session, kind="signal_validation", result={
            "verdict": verdict, "confidence": confidence, "score": None,
            "rationale": rationale, "model": cfg.validation_model, "payload": payload,
        }, signal_id=sig.id)

    try:
        result = await assessments.validate_and_correct_signal(cfg, _signal_dict(sig, source))
    except AiUnavailable as exc:
        # warning, not info: signal validation is the first line of defense, so a
        # systematic failure (e.g. an API 400) must be visible, not silent.
        log.warning("signal %s: AI validation unavailable — executing UNVALIDATED "
                    "on parser output: %s", sig.id, exc)
        _flag(sig, "unvalidated")
        await _store_row("unvalidated", None,
                         f"AI unavailable — executed on parser output. {exc}",
                         {"error": str(exc)})
        return "unvalidated"

    verdict = (result.get("verdict") or "").lower()
    payload = result.get("payload") or {}

    if not result.get("is_signal") or verdict == "reject":
        await _store_row(result.get("verdict") or "reject", result.get("confidence"),
                         result.get("rationale"), payload)
        sig.status = "rejected"
        sig.reject_reason = (result.get("rationale") or "AI rejected the signal")[:128]
        _flag(sig, "rejected")
        return "rejected"

    corr = _corrected_parsed(result.get("corrected") or {}, sig)
    ok_geom, geom_reason = (_geom_validate(corr) if corr else
                            (False, "AI returned unusable fields"))
    if not ok_geom:
        await _store_row(result.get("verdict"), result.get("confidence"),
                         result.get("rationale"), payload)
        sig.status = "rejected"
        sig.reject_reason = (geom_reason or "AI correction not tradeable")[:128]
        _flag(sig, "rejected")
        return "rejected"

    # Apply the corrected, geometry-checked signal.
    sig.symbol = corr.symbol
    sig.direction = corr.direction
    sig.entry_from = corr.entry_from
    sig.entry_to = corr.entry_to
    sig.sl = corr.sl
    sig.tps = [str(t) for t in corr.tps]
    if corr.order_type_hint:
        sig.order_type = corr.order_type_hint
    sig.status = "validated"
    _flag(sig, "validated", payload.get("corrections", []))
    await _store_row(result.get("verdict"), result.get("confidence"),
                     result.get("rationale"), payload)
    return "validated"


async def assess_execution(session: AsyncSession, sig: Signal,
                           source: Optional[Source], plan: dict, account_id: int,
                           cfg: Optional[AiConfig] = None
                           ) -> Optional[AiAssessment]:
    cfg = cfg or await load_config(session)
    if not (cfg.ready and cfg.review_execution):
        return None
    try:
        result = await assessments.review_execution(cfg, _signal_dict(sig, source), plan)
    except AiUnavailable as exc:
        log.info("signal %s acct %s: AI exec review unavailable: %s",
                 sig.id, account_id, exc)
        return None
    return await _store(session, kind="execution_review", result=result,
                        signal_id=sig.id, account_id=account_id)


async def assess_outcome(session: AsyncSession, trade_dict: dict, trade_id: int,
                         cfg: Optional[AiConfig] = None) -> Optional[AiAssessment]:
    cfg = cfg or await load_config(session)
    if not (cfg.ready and cfg.analyze_outcomes):
        return None
    try:
        result = await assessments.analyze_outcome(cfg, trade_dict)
    except AiUnavailable as exc:
        log.info("trade %s: AI outcome analysis unavailable: %s", trade_id, exc)
        return None
    return await _store(session, kind="outcome_analysis", result=result,
                        trade_id=trade_id)
