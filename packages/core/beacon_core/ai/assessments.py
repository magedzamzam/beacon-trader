"""Signal / execution / outcome assessments backed by an LLM.

Each function takes plain dicts (no ORM objects) and returns a normalized
result: {verdict, confidence, score, rationale, payload, model}. Callers persist
these into the AiAssessment table. Every function raises AiUnavailable when the
model can't be reached — the trading path treats that as "no verdict".
"""
from __future__ import annotations

from typing import Optional

from .config import AiConfig
from .provider import AiUnavailable, structured_call

__all__ = ["validate_signal", "review_execution", "analyze_outcome", "AiUnavailable"]

_SYSTEM = (
    "You are a disciplined risk manager embedded in an automated trading "
    "platform. You review trading signals and executions for coherence, risk "
    "quality, and red flags. You are conservative: capital preservation first. "
    "You never invent facts not present in the input. Respond ONLY with the "
    "requested JSON."
)

_SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "caution", "reject"]},
        "confidence": {"type": "number"},
        "quality_score": {"type": "number"},
        "risk_reward": {"type": "number"},
        "rationale": {"type": "string"},
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "confidence", "quality_score", "rationale", "flags"],
    "additionalProperties": False,
}

_EXEC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "caution", "reject"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "confidence", "rationale", "concerns"],
    "additionalProperties": False,
}

_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "mixed", "bad"]},
        "execution_score": {"type": "number"},
        "rationale": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "execution_score", "rationale", "lessons"],
    "additionalProperties": False,
}


def _clamp01(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


async def validate_signal(cfg: AiConfig, signal: dict) -> dict:
    """Judge a signal's coherence and risk quality before it trades."""
    user = (
        "Assess this trading signal.\n\n"
        f"Source: {signal.get('source_name', 'unknown')} "
        f"(kind: {signal.get('source_kind', 'unknown')})\n"
        f"Symbol: {signal.get('symbol')}\n"
        f"Direction: {signal.get('direction')}\n"
        f"Entry: {signal.get('entry_from')} to {signal.get('entry_to')}\n"
        f"Stop loss: {signal.get('sl')}\n"
        f"Take profits: {signal.get('tps')}\n"
        f"Order type: {signal.get('order_type')}\n"
        f"Raw text: {signal.get('raw_text') or '(none)'}\n\n"
        "Consider: is the geometry sound (SL and TPs on the correct side of "
        "entry)? Is the risk:reward reasonable? Are stops/targets plausibly "
        "placed? Any signs this is noise, spam, or malformed? Give a verdict "
        "(approve/caution/reject), confidence 0-1, a 0-100 quality score, the "
        "primary risk:reward ratio to the first TP, and concise flags."
    )
    data = await structured_call(cfg, system=_SYSTEM, user=user, schema=_SIGNAL_SCHEMA)
    return {
        "verdict": data.get("verdict"),
        "confidence": _clamp01(data.get("confidence")),
        "score": data.get("quality_score"),
        "rationale": data.get("rationale"),
        "model": data.get("_model", cfg.model),
        "payload": data,
    }


async def review_execution(cfg: AiConfig, signal: dict, plan: dict) -> dict:
    """Sanity-check a sized execution plan for one account before placing it."""
    legs_txt = "\n".join(
        f"  - TP{l.get('tp_index')}: entry {l.get('entry')} tp {l.get('tp')} "
        f"sl {l.get('sl')} lot {l.get('lot')}"
        for l in plan.get("legs", [])
    ) or "  (no legs)"
    user = (
        "A validated signal is about to be executed on a broker account. "
        "Sanity-check the sized plan before it is placed.\n\n"
        f"Symbol: {signal.get('symbol')}  Direction: {signal.get('direction')}\n"
        f"Account currency: {plan.get('account_currency')}  "
        f"Equity: {plan.get('equity')}\n"
        f"Planned total risk: {plan.get('planned_risk')} "
        f"({plan.get('risk_pct')}% of equity)\n"
        f"Legs:\n{legs_txt}\n\n"
        "Consider: is total risk sane for the equity? Are lot sizes plausible? "
        "Any leg whose stop distance or size looks wrong? Return a verdict "
        "(approve/caution/reject), confidence 0-1, and specific concerns."
    )
    data = await structured_call(cfg, system=_SYSTEM, user=user, schema=_EXEC_SCHEMA)
    return {
        "verdict": data.get("verdict"),
        "confidence": _clamp01(data.get("confidence")),
        "score": None,
        "rationale": data.get("rationale"),
        "model": data.get("_model", cfg.model),
        "payload": data,
    }


async def analyze_outcome(cfg: AiConfig, trade: dict) -> dict:
    """Post-mortem a closed trade: was execution good, and what to learn."""
    legs_txt = "\n".join(
        f"  - TP{l.get('tp_index')}: outcome {l.get('outcome')} "
        f"entry {l.get('entry')} close {l.get('close_price')} "
        f"pl {l.get('realized_pl')}"
        for l in trade.get("legs", [])
    ) or "  (no legs)"
    user = (
        "Review this closed trade and its execution.\n\n"
        f"Symbol: {trade.get('symbol')}  Direction: {trade.get('direction')}\n"
        f"Planned risk: {trade.get('planned_risk')}  "
        f"Realized P&L: {trade.get('realized_pl')}\n"
        f"Source: {trade.get('source_name', 'unknown')}\n"
        f"Legs:\n{legs_txt}\n\n"
        "Consider: did the outcome match the plan? Was risk managed well "
        "(stops moved, partials taken)? What, if anything, should change next "
        "time? Return a verdict (good/mixed/bad), a 0-100 execution score, a "
        "concise rationale, and a few concrete lessons."
    )
    data = await structured_call(cfg, system=_SYSTEM, user=user, schema=_OUTCOME_SCHEMA)
    return {
        "verdict": data.get("verdict"),
        "confidence": None,
        "score": data.get("execution_score"),
        "rationale": data.get("rationale"),
        "model": data.get("_model", cfg.model),
        "payload": data,
    }
