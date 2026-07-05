"""Anthropic provider: one structured-JSON call, async, defensive.

Uses the official Anthropic SDK (AsyncAnthropic) and constrains the response to
a JSON schema via output_config so callers get a validated dict back. All
failures surface as AiUnavailable — the trading path treats that as "no verdict"
rather than an error.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from ..logging import get_logger
from .config import AiConfig

log = get_logger("ai")


class AiUnavailable(Exception):
    """AI could not produce a verdict (no key, transport error, bad output)."""


# `output_config.effort` is rejected with HTTP 400 by models that don't support
# it — Haiku (all tiers) and Sonnet 4.5 and older. Opus 4.5+, Sonnet 4.6+,
# Sonnet 5, and Fable 5 accept it. Deny-list the known-unsupported families so
# that new models default to "supported" rather than silently losing effort.
_NO_EFFORT_MARKERS = (
    "haiku", "sonnet-4-5", "sonnet-4-0", "sonnet-4-2025",
    "sonnet-3", "opus-3", "claude-3", "claude-2",
)


def _model_supports_effort(model: str) -> bool:
    m = (model or "").lower()
    return not any(marker in m for marker in _NO_EFFORT_MARKERS)


async def structured_call(
    cfg: AiConfig,
    *,
    system: str,
    user: str,
    schema: dict,
    effort: str = "low",
    max_tokens: int = 4000,
    model: Optional[str] = None,
    thinking: bool = True,
    timeout: Optional[float] = None,
) -> dict:
    """Run one Anthropic message call constrained to `schema`; return the dict.

    `model` overrides cfg.model (e.g. a fast model for hot-path validation).
    `thinking=False` disables extended thinking (faster). `timeout` (seconds)
    caps the call — on expiry the call raises AiUnavailable so the caller can
    degrade gracefully. Raises AiUnavailable on any failure.
    """
    if not cfg.ready:
        raise AiUnavailable("AI is not enabled or no API key is configured")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise AiUnavailable("anthropic SDK not installed") from exc

    model_used = model or cfg.model
    # Only send `effort` to models that accept it — otherwise the API 400s and
    # the whole call degrades to "unavailable" (e.g. the fast Haiku validation
    # model rejects the effort parameter).
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if effort and _model_supports_effort(model_used):
        output_config["effort"] = effort
    kwargs = dict(
        model=model_used,
        max_tokens=max_tokens,
        output_config=output_config,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    client = anthropic.AsyncAnthropic(api_key=cfg.api_key)
    try:
        create = client.messages.create(**kwargs)
        resp = await (asyncio.wait_for(create, timeout=timeout) if timeout else create)
    except asyncio.TimeoutError as exc:
        raise AiUnavailable(f"AI timed out after {timeout}s") from exc
    except anthropic.APIStatusError as exc:
        raise AiUnavailable(f"AI API error {exc.status_code}: {exc.message}") from exc
    except Exception as exc:  # network, transport, etc.
        raise AiUnavailable(f"AI call failed: {exc}") from exc
    finally:
        try:
            await client.close()
        except Exception:
            pass

    if getattr(resp, "stop_reason", None) == "refusal":
        raise AiUnavailable("AI declined to respond to this request")

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise AiUnavailable("AI returned no text content")
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise AiUnavailable(f"AI returned non-JSON output: {text[:200]}") from exc
    data.setdefault("_model", model_used)
    return data
