# `notifications/` — operational alerts

Multi-channel operational notifications (new signal, order placed, TP/SL hit,
trade closed, errors…). **Best-effort and fully isolated** — a failing channel
never raises to the caller, because trading must never be affected by a
notification.

| File | Purpose |
|------|---------|
| `config.py` | Channel definitions + the event catalog, a sanitizer, and a secret-masked view for the UI. Pure — no DB/network/crypto; secret fields are stored opaque. |
| `dispatch.py` | Resolve routing + decrypt channel secrets, format the message for the event, and send. Swallows channel failures. |
| `senders.py` | Per-channel senders (e.g. Telegram bot, webhook). Each takes a resolved config (secrets already decrypted) + subject/text and raises on failure (caught upstream). |

**How it fits:** services call a small `_notify(event_id, ctx)` helper that fires
dispatch in the background (`spawn_bg`). Channel config and secrets live in the
`settings` table, editable from the Notifications page; delivery is opt-in per
event per channel.
