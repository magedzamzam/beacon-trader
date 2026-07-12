# `trading_hours/` — sessions, holidays, news blackout

Read-only market-timing intelligence: which session is active, whether the market
is closed (weekend/holiday), and whether a high-impact economic event calls for a
news blackout. Currently informational — it reports; it does not (yet) block.

| File | Purpose |
|------|---------|
| `sessions.py` | Trading-session windows (Asia/London/NY/overlap), computed live in each market's local timezone so DST is handled automatically. Windows are configurable. |
| `holidays.py` | US market (NYSE) holiday calendar + weekend status — computed from fixed rules (nth-weekday, observed shifts, Good Friday via Easter), so any year is derivable with no external data. |
| `calendar.py` | Economic-calendar feed for the news blackout — isolated + swappable (default: the free ForexFactory weekly JSON mirror; override via env). |
| `config.py` | Trading-hours config defaults + sanitizer (pure — no DB/network). |
| `service.py` | Orchestration: load config, compute aggregate status, and refresh the calendar (with a stale-refresh timestamp). |

**How it fits:** the API exposes the aggregate status to the Trading Hours page;
the calendar refresh runs in the background. A future phase can promote the
blackout from "reported" to "enforced" without changing the computation.
