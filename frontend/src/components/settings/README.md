# `frontend/src/components/settings`

| File | Purpose |
|------|---------|
| `placeholders.jsx` | `PLACEHOLDERS` — the catalog of routable "planned but not built" Settings views (Integrations, Strategies, General, Users, API & Webhooks, Compliance, Backups, Billing). Each renders the shared `<Placeholder>` panel and is wired into `lib/nav.jsx` as a first-class view, so the platform's full intended shape is visible in the sidebar today. |

Replace an entry here with a real page (add it to `pages/` and point `PAGES` at
it) when that feature is built.
