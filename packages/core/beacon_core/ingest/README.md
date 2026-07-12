# `ingest/` — the one inbound pipeline

Message ingestion used to be implemented twice (Telegram vs the HTTP/webhook
router) with divergent dedupe keys and two `Signal` builders. This package
collapses them into a single provider-agnostic pipeline behind a common
contract, mirroring the `BrokerAdapter` pattern on the execution side.

| File | Purpose |
|------|---------|
| `base.py` | `InboundMessage` + `IngestResult` contracts and the `BaseInboundChannel` ABC — a channel owns its transport and normalizes raw provider messages into `InboundMessage`. |
| `pipeline.py` | `ingest_message(session, msg)` — the single pipeline: parse → validate → dedupe (one structured-identity key for every channel) → persist `Signal` (+ optional channel message row) → AI-validate free-text → publish to the durable queue. |
| `registry.py` | `get_channel(kind, config)` / `register_channel()` — factory so a host (e.g. a collector) can build N channels by `Source.kind`. |

**How it fits:** the Telegram service registers a thin `TelegramInboundChannel`
and the API's `_ingest` router is a thin wrapper — both feed the same
`ingest_message`. One dedupe strategy, one `Signal` construction, one place to
change. Publishing lands on the durable Redis queue the `executor` consumes.
