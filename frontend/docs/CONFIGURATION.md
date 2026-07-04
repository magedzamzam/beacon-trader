# Configuration & Dashboard — structure and placeholders

This document describes the consolidated frontend navigation introduced to make the
platform feel like a stable, enterprise-grade trading system, and it catalogues every
**placeholder** so the remaining functionality can be implemented later.

## Navigation consolidation

The sidebar was collapsed from 13 flat items into three groups:

| Group    | Items |
|----------|-------|
| Overview | **Dashboard** |
| Live     | Positions, Signals, Chart, Messages, Activity, History |
| Settings | **Configuration** |

- **Dashboard** (`src/pages/Dashboard.jsx`) now surfaces all performance metrics
  (realized P&L, win rate, profit factor, open positions, total trades, closed legs)
  plus a *Performance by source* preview and *Recent trades*. Each card has a
  **View more →** link that navigates to the detailed page (`performance`, `history`).
  These detail pages are still routable (`App.jsx` `PAGES` map) but are no longer in
  the sidebar — they are reached through the dashboard.

- **Configuration** (`src/pages/Configuration.jsx`) is a single tabbed page. Tabs are
  grouped (Connectivity / Trading / Intelligence / Platform). On desktop the tabs are a
  vertical grouped list; on mobile they collapse to a horizontal scrollable strip.

## Functional tabs (already working)

These tabs render the existing, fully-functional feature components:

| Tab | Component | Status |
|-----|-----------|--------|
| Brokers & Accounts | `pages/Brokers.jsx` | ✅ Working (brokers + accounts + live fetch) |
| Signal Sources | `pages/Sources.jsx` | ✅ Working |
| Symbols | `pages/Symbols.jsx` | ✅ Working |
| Risk & Limits | `pages/Risk.jsx` | ✅ Working |
| AI Validation | `pages/AI.jsx` | ✅ Working |
| Currency & FX | `pages/settings/Currency.jsx` | 🟡 Partial — see below |

### Currency & FX — partial

`pages/settings/Currency.jsx` is functional but persists to **localStorage only**
(key `beacon_currency_prefs`). It sets base/reporting currency, display currency,
symbol position, thousands grouping, FX rate source, and auto-convert.

**To finish:** add a backend `GET/PUT /settings/currency` endpoint and swap the
`load()`/`save()` helpers for `api` calls. Then apply the base currency + FX rates in
`_useData.js`’s `money()` formatter (and dashboard KPIs) so figures actually convert.

## Placeholder tabs (not yet implemented)

Each placeholder renders `components/Placeholder.jsx` with a documented list of planned
capabilities. Below is the intended scope and the backend work each will need.

### Connectivity → Integrations
Additional broker adapters (MetaTrader, IBKR, OANDA, Binance), market-data vendor keys,
outbound sync (Sheets/Notion/Airtable), Zapier/Make catalog, per-integration health.
**Backend:** integration registry + adapter interface, encrypted credential storage,
health-poll endpoints.

### Trading → Strategies
Named strategy templates (scalp/swing/DCA/grid), partial-TP ladders, trailing-stop and
break-even presets, per-source assignment, backtest against history.
**Backend:** `strategies` table + CRUD, link to `sources.strategy`, backtest runner.

### Trading → Trading Hours  ✅ built (intelligence layer)
**Implemented (read-only status + config):**
- **Session windows** (Asian/London/New York) — configured in each market's local time,
  DST handled via `zoneinfo`; computed live (no external data). Active sessions + next
  open/close boundary are exposed. `beacon_core/trading_hours/sessions.py`.
- **News blackout** — high-impact economic calendar fetched from a free feed (ForexFactory
  mirror, swappable via `TRADING_HOURS_CALENDAR_URL`), persisted to `econ_events`, refreshed
  when stale. `blackout_status` gives `in_blackout` + next event.
  `beacon_core/trading_hours/{calendar,service}.py`.
- **Weekend & US holidays** — computed live (NYSE rules incl. Good Friday via Easter,
  observed shifts). `beacon_core/trading_hours/holidays.py`.
- Config stored in the `trading_hours` setting; status at `GET /trading-hours/status`;
  shown on the Trading Hours tab and a Dashboard strip. **Nothing gates trades yet.**

**Deferred — to implement later (the operator asked to document these):**
- **Enforcement / gating**: honour `sessions[].enabled`, `news.enabled`, `holidays.block_*`
  in the executor before placing (skip / queue / halve). A `Source.strategy` or account-level
  policy chooses the action, mirroring `regime_policy` from the reverted plan.
- **Daily max-trades**: per account/source cap on trades opened per UTC day → halt for the
  day once hit (Event `daily_cap`). Needs a per-day counter keyed on account/source.
- **Cool-down between entries**: minimum minutes between opening trades on the same
  symbol/source; drop or queue signals inside the window. Store `last_entry_ts` per key.
- **Timezone-aware scheduling**: arbitrary allow/deny windows beyond the 3 default sessions
  (e.g. custom "no Fridays after 20:00 UTC"), and per-source schedule overrides.
**Backend for enforcement:** a small gate in `services/executor` reading
`trading_hours.status()` + the counters, writing a skip Event with the reason.

### Intelligence → Notifications
Channels (email, Telegram, Slack, Discord, SMS, webhook), per-event routing, severity
thresholds, quiet hours, digest scheduling, broker-drop escalation.
**Backend:** `notification_channels` + `notification_rules`, an event bus, senders.

### Platform → General
Platform name/logo/brand color, default timezone/locale, default theme, number/date
formatting, data retention.
**Backend:** `GET/PUT /settings/general`.

### Platform → Users & Roles
Invite members, RBAC roles (Admin/Trader/Analyst/Read-only), 2FA enforcement, SSO/SAML,
per-account/broker scoping, session/device management.
**Backend:** `users`, `roles`, `memberships`; extend auth (currently single-user login)
to multi-user + permission checks on every endpoint.

### Platform → API & Webhooks
Scoped personal access tokens with expiry, inbound webhook endpoints per source with
signing secrets, rate limits + IP allow-list, key rotation/revocation with audit,
OpenAPI reference + request logs.
**Backend:** `api_tokens` table, token-auth middleware, webhook signature verification.

### Platform → Compliance & Audit
Immutable audit log of config/trade actions, trade blotter export (CSV/PDF), regulatory
/ tax reports, data residency + retention, per-user activity trail.
**Backend:** append-only `audit_log`, export endpoints, report generators.

### Platform → Backups & Data
Scheduled encrypted DB backups, point-in-time restore, config export/import bundle,
`SECRET_KEY` management + rotation, retention per data class.
**Backend:** backup scheduler + storage target, restore tooling, config (de)serializer.

### Platform → Billing & Usage
Subscription plan + limits, usage metering (API calls, AI tokens, active accounts),
invoices, payment methods, cost alerts/caps, per-team allocation.
**Backend:** metering counters, billing provider integration (e.g. Stripe).

### Platform → System Health
Service status (API, worker, DB, broker links), queue depth + latency, build/version,
restart / maintenance-mode controls, live log tail + error rate.
**Backend:** `/health` already exists — extend to per-service checks, metrics, log tail.

## Where to change things

- Add/rename a tab → edit the `GROUPS` array in `src/pages/Configuration.jsx`.
- Turn a placeholder into a real feature → replace its `render: () => <Placeholder .../>`
  with `render: () => <YourComponent />` and delete its entry from this doc's placeholder list.
- Sidebar groups → `NAV` in `src/components/Layout.jsx`.
