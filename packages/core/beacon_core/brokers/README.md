# `brokers/` — broker gateway

A clean adapter interface so the rest of the platform never imports a broker SDK
directly. Adding a broker is one adapter class + one registry entry; nothing
downstream changes.

| File | Purpose |
|------|---------|
| `base.py` | `BrokerAdapter` abstract base — the methods every broker must implement (quote, bars, place/modify/cancel, positions, orders, account info, healthcheck). |
| `types.py` | Broker-agnostic data contracts every adapter speaks in: `PlaceOrderRequest`, `BrokerOrder/Position/Quote`, `AccountInfo`, `OrderSide/Type/Status`, and the error hierarchy (`AuthError`, `RateLimitError`, `NetworkError`, …). |
| `capital_com.py` | The Capital.com REST adapter — session login with 429 back-off, account switching, order placement (MARKET + working orders with broker-enforced `goodTillDate`), bars, transactions/activity, healthcheck (with latency). |
| `registry.py` | `get_adapter(type, creds)` maps a broker-type string to an adapter class; `resolve_credentials()` decrypts the stored `credentials_ref` (Fernet, never `.env`). |
| `factory.py` | Session-aware construction: `build_adapter(session, account)` → `(broker, adapter)` bound to a mapped account; `make_adapter(broker)` for connection tests; `symbol_map()` for the shared SymbolMap lookup. |
| `fx.py` | Currency conversion resolved **live from the broker's own FX markets** (no hardcoded rate) so a non-USD account is sized correctly. |

**Key idea:** everything above the adapter deals only in `types.py` contracts, so
the executor/monitor/API are broker-independent. Credentials are always resolved
through `resolve_credentials` (returns a fresh dict; never shares session state
between adapters).
