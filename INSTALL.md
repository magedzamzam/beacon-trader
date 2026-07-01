# Beacon Trader — Install & Operate (Oracle Linux 9)

Runbook from a bare VM to a running portal, plus the `git pull` update flow.

## 0. Prerequisites

- Oracle Linux 9 host (an OCI free-tier ARM VM is fine).
- A **managed PostgreSQL** you can reach (connection string + user/password).
- **Capital.com** API key + username + password (use a **DEMO** key first).
- **Telegram** `api_id` + `api_hash` from https://my.telegram.org, and access to
  the channels you want to follow.

## 1. Install Docker + Compose

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"   # log out/in so the group applies
docker compose version            # verify
```

Open the ports you need (portal 8080, API 8000) in the OCI security list /
firewall as appropriate.

## 2. Clone

```bash
git clone https://github.com/<you>/beacon-trader.git
cd beacon-trader
```

## 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

- `DATABASE_URL` — your managed Postgres, **asyncpg** form:
  `postgresql+asyncpg://USER:PASSWORD@HOST:5432/beacon`
- `API_TOKEN` — a long random string (this is your portal login token):
  `openssl rand -hex 24`
- `CAP_API_KEY`, `CAP_USERNAME`, `CAP_PASSWORD` — Capital.com (demo first).
- `TG_API_ID`, `TG_API_HASH` — from my.telegram.org. (`TG_SESSION` comes next.)
- `VITE_API_BASE` — the URL the browser uses to reach the API,
  e.g. `http://YOUR_HOST:8000`.

Secrets live only in `.env` (git-ignored). The database stores *references* to
these env vars, never the secrets themselves.

## 4. Mint the Telegram session (once)

```bash
docker compose run --rm telegram python login.py
```

Log in when prompted; copy the printed session string into `.env` as
`TG_SESSION=...` (single line).

## 5. Launch

```bash
docker compose up -d --build
```

## 6. Seed baseline data

Creates a demo broker (credentials via `.env`), an account, an XAUUSD symbol
map, and sample sources:

```bash
docker compose run --rm api python -m app.seed
# or: make seed
```

## 7. Verify

```bash
curl -s localhost:8000/health | python3 -m json.tool   # or: make health
```

Expect `"ok": true` with `database`, `redis`, and the three workers healthy.
Open the portal at `http://YOUR_HOST:8080`, click the key icon, paste your
`API_TOKEN`, Save.

## 8. Go live on your data

In the portal (or via the API):

1. **Brokers** — confirm the seeded Capital.com broker; keep **DEMO** on.
2. **Symbols** — set `value_per_point` to your broker's real gold value
   (see README calibration note), plus `min_lot` / `lot_step` /
   `min_stop_distance`.
3. **Sources** — for each Telegram channel, set `external_id` to the channel id
   (e.g. `-1001220837618`), pick `order_position_type`, a `tp_strategy`, SL
   rules, `risk_config`, and the `account_map`. Flip `enabled_for_trading` on
   only when you're ready.
4. Send a test into the **Manual Desk** source:

```bash
curl -X POST localhost:8000/signals/manual \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"source_id":2,"symbol":"XAUUSD","direction":"BUY",
       "entry_from":4105,"entry_to":4102,"sl":4098,
       "tps":[4110,4112,4114],"order_type":"MARKET"}'
```

Watch **Signals → Positions** populate, and **monitor** logs move stops.

TradingView / generic webhook (auth = the source's `external_id` as key):

```bash
curl -X POST localhost:8000/ingest/tv/<source_external_id> \
  -H "Content-Type: application/json" \
  -d '{"text":"XAUUSD BUY 4105-4102 TP1 4110 TP2 4112 SL 4098"}'
```

## Updating (the git workflow)

```bash
cd beacon-trader
git pull
docker compose up -d --build      # or: make up
```

Schema changes are applied idempotently on startup (Phase 1 uses
`create_all`; add Alembic when you need destructive migrations).

## Operating

```bash
make logs     # tail everything      make ps    # status
make down     # stop                 make up    # rebuild + start
docker compose logs -f executor      # one service
```

## Security housekeeping

- Never commit `.env`, `*.session`, or `*.pem` (already git-ignored).
- If you reused material from an older bot, **rotate any leaked Telegram
  session, bot token, or API keys** — treat anything that was ever in a shared
  archive as compromised.
- Put the API behind TLS / a reverse proxy before exposing it publicly; the
  Phase-1 token auth is single-user and minimal.
