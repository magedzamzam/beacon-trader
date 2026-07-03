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

__all__ = ["validate_signal", "validate_and_correct_signal", "review_execution",
           "analyze_outcome", "AiUnavailable"]

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

_CORRECT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_signal": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["approve", "caution", "reject"]},
        "confidence": {"type": "number"},
        "symbol": {"type": "string"},
        "direction": {"type": "string", "enum": ["BUY", "SELL"]},
        "entry_from": {"type": "number"},
        "entry_to": {"type": "number"},
        "sl": {"type": "number"},
        "tps": {"type": "array", "items": {"type": "number"}},
        "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"]},
        "corrections": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["is_signal", "verdict", "confidence", "symbol", "direction",
                 "entry_from", "entry_to", "sl", "tps", "order_type",
                 "corrections", "rationale"],
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


async def validate_and_correct_signal(cfg: AiConfig, signal: dict) -> dict:
    """Validate a free-text-parsed signal against its ORIGINAL message and return
    a corrected structured signal. The local parser can misread things (e.g. a
    pip distance like '(1540 pips)' captured as a take-profit); the model uses the
    raw message as the source of truth and fixes the fields. Runs on the fast
    validation model with a hard timeout so it stays under a few seconds."""
    user = (
        "A trading signal was auto-parsed from a chat message. The local parser "
        "can make mistakes — for example reading a pip/point distance shown in "
        "parentheses like '(1540 pips)' as a take-profit level. Using the ORIGINAL "
        "message as the source of truth, return the CORRECT structured signal.\n\n"
        f"Original message:\n{signal.get('raw_text') or '(none)'}\n\n"
        "Parser interpretation (may be wrong):\n"
        f"  Symbol: {signal.get('symbol')}\n"
        f"  Direction: {signal.get('direction')}\n"
        f"  Entry: {signal.get('entry_from')} to {signal.get('entry_to')}\n"
        f"  Stop loss: {signal.get('sl')}\n"
        f"  Take profits: {signal.get('tps')}\n"
        f"  Order type: {signal.get('order_type')}\n\n"
        "Rules:\n"
        "- Use ONLY price levels actually stated in the message.\n"
        "- Ignore pip/point distances and any parenthetical or non-price annotations.\n"
        "- Keep the stop loss and EVERY take-profit on the correct side of entry "
        "for the direction (SELL: SL above, TPs below; BUY: SL below, TPs above).\n"
        "- Preserve the stated entry (or entry zone) unless the message is explicit.\n"
        "- Return take-profits in the order given.\n"
        "- If the message is NOT actually a tradeable signal, set is_signal=false "
        "and verdict=reject.\n\n"
        "Return the corrected symbol, direction, entry_from, entry_to, sl, tps, "
        "order_type, the list of corrections you made, a verdict "
        "(approve/caution/reject) and confidence 0-1."
    )
    data = await structured_call(
        cfg, system=_SYSTEM, user=user, schema=_CORRECT_SCHEMA,
        model=cfg.validation_model, thinking=cfg.validation_thinking,
        timeout=cfg.validation_timeout_seconds, max_tokens=1500,
    )
    return {
        "is_signal": bool(data.get("is_signal")),
        "verdict": data.get("verdict"),
        "confidence": _clamp01(data.get("confidence")),
        "corrected": {
            "symbol": data.get("symbol"),
            "direction": (data.get("direction") or "").upper(),
            "entry_from": data.get("entry_from"),
            "entry_to": data.get("entry_to"),
            "sl": data.get("sl"),
            "tps": data.get("tps") or [],
            "order_type": (data.get("order_type") or "MARKET").upper(),
        },
        "rationale": data.get("rationale"),
        "model": data.get("_model", cfg.validation_model),
        "payload": data,
    }


async def review_execution(cfg: AiConfig, signal: dict, plan: dict) -> dict:
    """Sanity-check a sized execution plan for one account before placing it."""
    legs_txt = "\n".join(
        f"  - TP{l.get('tp_index')}: entry {l.get('entry')} tp {l.get('tp')} "
        f"sl {l.get('sl')} lot {l.get('lot')}"
        for l in plan.get("legs", [])
    ) or "  (no legs)"
    acct_ccy = plan.get("account_currency")
    instr_ccy = plan.get("instrument_currency")
    vpp = plan.get("value_per_point")
    fx = plan.get("fx_factor")
    user = (
        "A validated signal is about to be executed on a broker account. "
        "Sanity-check the sized plan before it is placed.\n\n"
        f"Symbol: {signal.get('symbol')}  Direction: {signal.get('direction')}\n"
        f"Account currency: {acct_ccy}   Equity: {plan.get('equity')} {acct_ccy}\n"
        f"Instrument quote currency: {instr_ccy}\n"
        f"value_per_point: {vpp} {instr_ccy} — money gained/lost per 1.0 price move "
        "per 1.0 lot. THIS defines the contract size; do NOT assume a standard "
        "100-unit/oz lot.\n"
        f"FX factor (account -> instrument): {fx}\n"
        f"Planned total risk: {plan.get('planned_risk')} {acct_ccy} "
        f"({plan.get('risk_pct')}% of equity)\n"
        f"Legs:\n{legs_txt}\n\n"
        "How sizing works — verify against this, do not reinvent it:\n"
        "  risk_in_instrument_ccy = |entry - sl| * lot * value_per_point\n"
        "  risk_in_account_ccy    = risk_in_instrument_ccy / fx_factor\n"
        "Equity and the planned risk shown above are ALREADY in account currency. "
        "Notional and exposure must be judged using value_per_point and the FX "
        "factor — do NOT assume the instrument is priced in the account currency, "
        "and do NOT assume 1 lot equals 100 units. A large lot count can be "
        "perfectly correct when value_per_point is small.\n\n"
        "Given that, consider: is total risk sane for the equity (account "
        "currency)? Are the lot sizes consistent with the stated risk, the stop "
        "distances and value_per_point? Only flag a problem if the numbers are "
        "genuinely inconsistent with the formula above. Return a verdict "
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
