# `frontend/src/lib` — app plumbing

Non-visual building blocks shared across pages.

| File | Purpose |
|------|---------|
| `api.js` | The single API client. A thin `fetch` wrapper that attaches the bearer token and JSON-decodes; exports one method per endpoint (`api.trades()`, `api.analyticsCorrelation(range)`, `api.saveRiskLimits(cfg)`, …). Also the token helpers (`getToken`/`setToken`/`clearToken`) and the `_perfQs` date-range query builder. |
| `nav.jsx` | **Single source of truth for navigation + routing.** `NAV` (sidebar hierarchy with expandable Settings subgroups), `PAGES` (`id → component`), `REDIRECTS` (legacy ids), and `leafLabel`/`parentTitleOf` helpers. Both `Layout` and `App` consume it. |
| `theme.js` | Light/dark theme toggle (persists choice; stamps `data-theme` on the root). |

To add a destination: import the page here, add one `NAV` leaf + one `PAGES`
entry — the sidebar and router pick it up automatically.
