# `telegram/` — channel listener & ingest

A Telethon **user** (MTProto) session that watches the channels configured as
`telegram` Sources. **Every** message on a watched channel is persisted (signal
or not) so the portal shows the complete per-channel history.

| File | Purpose |
|------|---------|
| `login.py` | One-time interactive login that produces the `TG_SESSION` string (run once; store the session, never the password). |
| `main.py` | The listener + backfill + control loop. |

## What it does
- **Live listen** — each new message is normalized into an `InboundMessage` and
  run through the shared `beacon_core.ingest` pipeline (parse → validate →
  dedupe → persist `TelegramMessage`/`Signal` → publish). Telegram is free-text,
  so the pipeline AI-validates/corrects before a signal can trade.
- **Backfill** — on startup and on demand (via the `telegram.control` Redis
  channel), pulls recent history so a fresh portal isn't empty. Backfilled
  signals are recorded as `history` and **never** traded.
- **Abstraction** — exposes a thin `TelegramInboundChannel(BaseInboundChannel)`
  and registers it, so the ingestion logic is shared with the API's webhook path
  rather than duplicated.

Requires `TG_API_ID` / `TG_API_HASH` / `TG_SESSION` (see `login.py`). If unset,
the service still serves `/healthz` and reports the missing config.
