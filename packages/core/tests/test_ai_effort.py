"""Regression test for issue #11 — the signal-validation AI 400'd because it
sent `output_config.effort` to a model (Haiku 4.5) that rejects the parameter.

`effort` must be gated by model capability: sent to Opus 4.5+, Sonnet 4.6+,
Sonnet 5, Fable 5; omitted for Haiku (all tiers) and Sonnet 4.5 / older.
"""
from beacon_core.ai.provider import _model_supports_effort


def test_effort_supported_models():
    for m in ("claude-opus-4-8", "claude-opus-4-5", "claude-opus-4-7",
              "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5"):
        assert _model_supports_effort(m) is True, m


def test_effort_unsupported_models():
    # the validation-path default and other known-unsupported families
    for m in ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-3-5-haiku",
              "claude-sonnet-4-5", "claude-sonnet-4-0", "claude-3-opus"):
        assert _model_supports_effort(m) is False, m


def test_effort_gating_none_and_empty():
    assert _model_supports_effort("") is True     # unknown -> default supported
    assert _model_supports_effort(None) is True
