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
    # per-stage toggles
    validate_signals: bool = True       # assess signals as they arrive
    review_execution: bool = True       # sanity-check the plan before placing
    analyze_outcomes: bool = True       # post-mortem closed trades
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
    cfg = AiConfig(
        enabled=bool(stored.get("enabled", False)),
        provider=stored.get("provider", "anthropic"),
        model=stored.get("model") or settings.ai_default_model,
        validate_signals=bool(stored.get("validate_signals", True)),
        review_execution=bool(stored.get("review_execution", True)),
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
