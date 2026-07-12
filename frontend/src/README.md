# `frontend/src` — app source

| Path | Purpose |
|------|---------|
| `main.jsx` | React entry point; mounts `<App/>`. |
| `App.jsx` | Auth gate + router: holds the active `view` id and renders `PAGES[view]` inside `<Layout>`. |
| `index.css` | Tailwind layers + theme CSS variables (light/dark tokens). |
| `lib/` | Non-visual app plumbing — see `lib/README.md`. |
| `components/` | Shared, reusable UI — see `components/README.md`. |
| `pages/` | One component per destination (Dashboard, Positions, Analytics, …) — see `pages/README.md`. |

## How routing works (no react-router)
Navigation is a single source of truth in `lib/nav.jsx`:
- `NAV` — the sidebar hierarchy (groups → items; Settings items have expandable
  `children`).
- `PAGES` — `{ leaf-id → component }`.

`Layout` renders the sidebar from `NAV` and calls `setView(id)`; `App` renders
`PAGES[view]`. Adding a page = add its component + one `NAV`/`PAGES` entry. Data
comes from `lib/api.js` (thin fetch client with the bearer token); most pages use
the `pages/_useData.js` hook or a shared `RangeFilter` for date scoping.
