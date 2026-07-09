"""Resolve the effective AI configuration.

Layered: hard defaults <- DB `ai` setting (editable from the UI) <- env.
The API key is resolved last, preferring an encrypted key stored from the UI,
then the ANTHROPIC_API_KEY env var.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import get_settings
from ..crypto import decrypt, is_encrypted


@dataclass
class AiConfig:
    enabled: bool = False               # master switch for AI assessments
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"      # used for execution review + outcome analysis
    # per-stage toggles (derived from the modes below; kept for existing checks
    # that just ask "is this stage enabled at all")
    validate_signals: bool = True       # assess signals as they arrive
    review_execution: bool = True       # sanity-check the plan before placing
    analyze_outcomes: bool = True       # post-mortem closed trades
    # Hot-path AI modes: "off" | "block" | "background".
    #   block      — run the AI and WAIT for it before publishing / placing (adds
    #                 latency; can correct/gate). Today's behaviour.
    #   background — publish / place immediately, then run the AI for the record
    #                 only (no correction, no gate) — removes the latency.
    #   off        — don't run it at all.
    validation_mode: str = "block"      # signal validation (Telegram hot path)
    review_mode: str = "block"          # execution review (executor hot path)
    # gate: if True, a `reject` verdict blocks execution
    gate_execution: bool = False
    min_confidence: float = 0.0         # gate only fires at/above this confidence
    # Signal validation/correction runs on the hot path (a signal must be
    # validated before it can trade), so it uses its own fast model, has extended
    # thinking off by default, and a hard timeout — tuned for sub-5s replies.
    validation_model: str = "claude-haiku-4-5-20251001"
    validation_timeout_seconds: float = 5.0
    validation_thinking: bool = False
    api_key: Optional[str] = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.api_key)

    def public_dict(self) -> dict:
        """UI-safe view — never leaks the key, only whether one is set."""
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "validate_signals": self.validate_signals,
            "review_execution": self.review_execution,
            "validation_mode": self.validation_mode,
            "review_mode": self.review_mode,
            "analyze_outcomes": self.analyze_outcomes,
            "gate_execution": self.gate_execution,
            "min_confidence": self.min_confidence,
            "validation_model": self.validation_model,
            "validation_timeout_seconds": self.validation_timeout_seconds,
            "validation_thinking": self.validation_thinking,
            "has_api_key": bool(self.api_key),
        }


def resolve_ai_config(stored: Optional[dict]) -> AiConfig:
    """Build an AiConfig from the DB `ai` setting value (may be None)."""
    stored = stored or {}
    settings = get_settings()

    _defaults = AiConfig()

    def _mode(key, legacy_key):
        m = stored.get(key)
        if m in ("off", "block", "background"):
            return m
        # legacy: derive from the old boolean toggle (True -> block, False -> off)
        return "block" if stored.get(legacy_key, True) else "off"

    validation_mode = _mode("validation_mode", "validate_signals")
    review_mode = _mode("review_mode", "review_execution")

    cfg = AiConfig(
        enabled=bool(stored.get("enabled", False)),
        provider=stored.get("provider", "anthropic"),
        model=stored.get("model") or settings.ai_default_model,
        validation_mode=validation_mode,
        review_mode=review_mode,
        validate_signals=(validation_mode != "off"),
        review_execution=(review_mode != "off"),
        analyze_outcomes=bool(stored.get("analyze_outcomes", True)),
        gate_execution=bool(stored.get("gate_execution", False)),
        min_confidence=float(stored.get("min_confidence", 0.0) or 0.0),
        validation_model=stored.get("validation_model") or _defaults.validation_model,
        validation_timeout_seconds=float(
            stored.get("validation_timeout_seconds") or _defaults.validation_timeout_seconds),
        validation_thinking=bool(stored.get("validation_thinking", False)),
    )

    # Key: encrypted-in-DB wins, else env.
    stored_key = stored.get("api_key_enc")
    if stored_key:
        cfg.api_key = decrypt(stored_key) if is_encrypted(stored_key) else stored_key
    if not cfg.api_key:
        cfg.api_key = settings.anthropic_api_key or None
    return cfg
