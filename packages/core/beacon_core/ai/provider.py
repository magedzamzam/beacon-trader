"""Anthropic provider: one structured-JSON call, async, defensive.

Uses the official Anthropic SDK (AsyncAnthropic) and constrains the response to
a JSON schema via output_config so callers get a validated dict back. All
failures surface as AiUnavailable — the trading path treats that as "no verdict"
rather than an error.
"""
from __future__ import annotations

import json
from typing import Optional

from ..logging import get_logger
from .config import AiConfig

log = get_logger("ai")


class AiUnavailable(Exception):
    """AI could not produce a verdict (no key, transport error, bad output)."""


async def structured_call(
    cfg: AiConfig,
    *,
    system: str,
    user: str,
    schema: dict,
    effort: str = "low",
    max_tokens: int = 4000,
) -> dict:
    """Run one Anthropic message call constrained to `schema`; return the dict.

    Raises AiUnavailable on any failure so callers can degrade gracefully.
    """
    if not cfg.ready:
        raise AiUnavailable("AI is not enabled or no API key is configured")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise AiUnavailable("anthropic SDK not installed") from exc

    client = anthropic.AsyncAnthropic(api_key=cfg.api_key)
    try:
        resp = await client.messages.create(
            model=cfg.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIStatusError as exc:
        raise AiUnavailable(f"AI API error {exc.status_code}: {exc.message}") from exc
    except Exception as exc:  # network, timeout, etc.
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
    data.setdefault("_model", cfg.model)
    return data
