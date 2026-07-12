# `frontend/src/components` — shared UI

Reusable presentation pieces used across pages. No data fetching lives here
(pages own that); these render props.

| File | Purpose |
|------|---------|
| `Layout.jsx` | The app shell: expandable sidebar (from `lib/nav`), header (account selector, broker-status chip, theme/token/logout), and the mobile drawer. Owns collapse/expand state. |
| `ui.jsx` | Primitives: `Card`, `Table` (with the mobile horizontal-scroll wrapper), `Th`/`Td`, `Badge`, `KPI`, `Empty`. |
| `form.jsx` | Form primitives: `Field`, `Input`, `NumberInput`, `Toggle`, `Button`, `Modal`, `ErrorNote`. |
| `RangeFilter.jsx` | The shared date-range filter — `PRESETS`/`COARSE_PRESETS`, the `useRange()` hook (returns `{fromIso,toIso,range}`, UTC ISO), and the pill-bar/custom-picker UI. Used by Performance/Reconciliation/Analytics/Bayesian. |
| `LineChart.jsx` | Recharts line chart (equity curve, growth). |
| `TradeDetail.jsx` | Per-trade drill-down (legs, activity, events). |
| `RiskConfigEditor.jsx` / `SlRulesEditor.jsx` | Editors for the risk-config and SL-rule JSON. |
| `SessionStrip.jsx` / `SessionTimeline.jsx` | Trading-session status widgets. |
| `NewsCard.jsx` | Economic-calendar / blackout card. |
| `Placeholder.jsx` | The "planned but not built" panel for unimplemented Settings leaves. |
| `settings/placeholders.jsx` | The catalog of routable placeholder components (Integrations, Strategies, Users, …). |

The `Table` primitive supplies the `overflow-x-auto` wrapper so wide tables
scroll within their card on mobile; prefer it over a raw `<table>`.
