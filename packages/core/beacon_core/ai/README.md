# `ai/` — LLM validation layer

Provider-abstracted assessments of signals, executions, and outcomes. Every
verdict is structured and **auditable** (stored in `ai_assessments`). The layer
**degrades gracefully**: no key / unreachable API returns "no verdict" and the
trading path never depends on the AI being up.

| File | Purpose |
|------|---------|
| `provider.py` | Anthropic provider — one structured-JSON, async, defensive call via the official SDK (`AsyncAnthropic`). Default model `claude-opus-4-8`. |
| `assessments.py` | The three assessment functions (signal / execution / outcome). Take plain dicts (no ORM objects), return a normalized verdict (`approve\|caution\|reject`, confidence, quality score, rationale). |
| `config.py` | Resolve the effective AI config, layered: hard defaults ← DB `ai` setting (editable in the UI) ← env. |
| `service.py` | Orchestration glue: load config → run an assessment → persist the result. Shared by telegram/executor/monitor/api so there's one implementation. |

## The three surfaces
- **Signal validation** — as a signal arrives, judge coherence/geometry/RR/red
  flags. In BLOCK mode it can correct or reject before the signal is published.
- **Execution review** — before the executor places a sized plan, sanity-check
  total risk and lot sizes. With **gate execution** on, a confident `reject`
  blocks that account's trade (`ai_blocked` event).
- **Outcome analysis** — on close, review the execution and record lessons.

**How it fits:** the ingest pipeline and executor call `ai/service` at the
relevant point; it's opt-in per surface and always fail-open.
