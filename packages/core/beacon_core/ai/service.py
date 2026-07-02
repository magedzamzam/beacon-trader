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
from ..logging import get_logger
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
