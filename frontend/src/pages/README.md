# `frontend/src/pages` — one component per destination

Each file is a full page rendered by `App` when its `view` id is active. Pages
own their data fetching (via `lib/api.js` + the `_useData.js` hook) and compose
`components/` primitives.

## Overview & live trading
| Page | View | Purpose |
|------|------|---------|
| `Dashboard.jsx` | `dashboard` | KPI grid, halt/risk banners, growth curve, per-source stats, recent trades. |
| `Positions.jsx` | `positions` | Open positions/legs with close/cancel actions. |
| `Signals.jsx` | `signals` | Signal feed (filter by channel), AI verdicts, re-initiate. |
| `Chart.jsx` | `chart` | Candlestick chart + indicators. |
| `Messages.jsx` | `messages` | Full Telegram message history per channel. |
| `Activity.jsx` | `activity` | Append-only execution/event log. |
| `History.jsx` | `history` | Closed legs ledger. |

## Intelligence
| Page | View | Purpose |
|------|------|---------|
| `Analytics.jsx` | `analytics` | Shadow-sidecar correlation: channel×regime, FVG/OB & structure-vs-outcome, capture toggle. |
| `Analysis.jsx` | `analysis` | Bayesian win-rate table + P(win) scores. |
| `Reconciliation.jsx` | `reconciliation` | Channel-claimed vs bot-actual, by reason. |
| `Performance.jsx` | `performance` | P&L / win-rate / profit-factor with date range + per-source credible intervals. |

## Settings
| Page | View | Purpose |
|------|------|---------|
| `Brokers.jsx` | `brokers` | Brokers & accounts (creds, live-account fetch, test connection). |
| `Sources.jsx` `Symbols.jsx` | `sources` `symbols` | Signal sources & symbol mapping. |
| `Risk.jsx` | `risk` | Risk limits, kill-switch, per-account risk, trend-filter card. |
| `settings/Currency.jsx` | `currency` | Currency & FX overrides. |
| `AI.jsx` `Indicators.jsx` `Notifications.jsx` | `ai` `indicators` `notifications` | AI validation, TA indicator config, notification channels. |
| `TradingHours.jsx` | `hours` | Session status + news blackout. |
| `SystemHealth.jsx` | `system` | Service heartbeats + broker connectivity/latency. |
| `Login.jsx` | — | Token entry (pre-auth). |

`_useData.js` is the shared fetch/loading hook; `Placeholder`-backed views
(Integrations, Strategies, Users, API, Compliance, Backups, Billing) come from
`components/settings/placeholders.jsx`.
