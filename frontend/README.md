# `frontend/` — React trading terminal

A React + Vite + Tailwind single-page app served by nginx (see `Dockerfile` +
`nginx.conf`). Dark/light "trading terminal" UI. It is a **pure client of the
API** — no business logic lives here; every number comes from a `beacon_core`
endpoint.

## Build / config
| File | Purpose |
|------|---------|
| `Dockerfile` / `nginx.conf` | Multi-stage build → static assets served by nginx (which also proxies `/` API calls). |
| `vite.config.js` | Dev server + build config. |
| `tailwind.config.js` / `postcss.config.js` / `src/index.css` | Styling; theme tokens (`--beacon`, `--long`, `--short`, `--warn`) drive light/dark. |
| `package.json` / `package-lock.json` | Dependencies (React, Vite, lucide-react icons, Recharts). |
| `index.html` / `src/main.jsx` | Entry points. |
| `docs/CONFIGURATION.md` | The information architecture of the unified sidebar + the placeholder catalog. |

## Source (`src/`)
See `src/README.md`. In short: `App.jsx` routes a `view` id to a page via the
`lib/nav.jsx` registry; `components/` are shared UI; `lib/` is the API client +
nav tree + theme; `pages/` is one component per destination.

## Note on building here
This repo's dev/jump host has **no Node toolchain** — the frontend builds only in
CI/Docker (`frontend/Dockerfile`). JSX changes are verified by eye against the
existing patterns; the visual render happens in the built image.
