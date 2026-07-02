"""AI integration layer for Beacon Trader.

Three assessment surfaces, each returning a structured, auditable verdict:
  * validate_signal   — is this signal coherent and worth trading?
  * review_execution  — pre-trade sanity check on the sized plan
  * analyze_outcome   — post-trade review + learnings

All are provider-abstracted (Anthropic Claude today) and degrade gracefully:
if no API key is configured they return an `unavailable` verdict instead of
raising, so the trading path never depends on the AI being reachable.
"""
from .config import AiConfig, resolve_ai_config
from .assessments import (
    validate_signal, review_execution, analyze_outcome, AiUnavailable,
)

__all__ = [
    "AiConfig", "resolve_ai_config",
    "validate_signal", "review_execution", "analyze_outcome", "AiUnavailable",
]
