# Order & position tracking (Capital.com)

How Beacon links what it placed to what the broker actually did, and how it
detects fills, cancels and closes. This reflects the real Capital.com API field
relationships verified against live demo calls (2026-07-03).

## The two broker models

Beacon places one **leg** per (entry, TP). A leg is either:

1. **Working order** — a resting LIMIT/STOP order. It can be: **cancelled**,
   **converted to a position** (filled), or **still pending**.
2. **Position** — an open trade. It can be: **running**, **hit TP**, **hit SL**,
   or **hit break-even** (SL that was ratcheted to entry, then hit).

## The IDs and how they connect

### Placing a working order (`POST /workingorders`)
```
POST /workingorders                  -> { dealReference: "o_..." }
GET  /confirms/o_...                  -> { dealId: "…db46", ... }   # the WORKING ORDER id
```
We store `…db46` as `leg.broker_order_ref`, leg status = `working`.

When it fills, a **new position** appears in `GET /positions` with:
```
position.dealId          = "…db5f"   # the POSITION's own id
position.workingOrderId  = "…db46"   # points back to the working order
```
**Link key:** `position.workingOrderId == leg.broker_order_ref`. On the fill we
set `leg.broker_position_ref = position.dealId` (`…db5f`) and status = `open`.

### Placing a position directly (`POST /positions`, MARKET)
```
POST /positions                      -> { dealReference: "o_..." }
GET  /confirms/o_...                  -> { dealId: "…da7f",
                                           affectedDeals: [ { dealId: "…da80", status: "OPENED" } ] }
```
The **real position id is in `affectedDeals[].dealId`** (`…da80`), *not* the
top-level `dealId` (`…da7f`, which becomes that position's `workingOrderId`).
We store `…da80` as `leg.broker_position_ref`, status = `open`.

### Closing a position — realized P&L
```
GET /history/transactions            -> transactions[] where note = "Trade closed"
  {
    dealId:  "…da80",     # == the POSITION dealId -> matches leg.broker_position_ref exactly
    size:    "4.85",      # the REALIZED P&L amount in ACCOUNT currency (NOT a lot size)
    currency:"AEDd",      # account currency
    note:    "Trade closed",
    transactionType: "TRADE"
  }
```
**Match key:** `transaction.dealId == leg.broker_position_ref`. Realized P&L is
`size` (preferring `profitAndLoss` if a deployment provides it). This API variant
does **not** return a close level, so the ledger close price is derived from the
live price vs. the leg's TP/SL at detection.

## How the monitor reconciles each tick

For every open leg of an open trade:

- **working, still in `GET /workingorders`** → pending; only the TTL rule can
  cancel it.
- **working, gone from the order book** →
  1. `position.workingOrderId == leg.broker_order_ref` → **filled** (link the
     position id). *ID-first.*
  2. else epic+direction heuristic (fallback only; ambiguous with multi-leg
     fan-outs, so it never runs before the ID match).
  3. else a confirmed close in history → **filled+closed** in one tick.
  4. else → **cancelled**.
- **open, gone from `GET /positions`** → **closed**. Match the closing
  transaction by dealId, take realized P&L from it, classify the outcome.

### Outcome classification (truth-first)
The broker's own reason for a close is the source of truth, read from
`GET /history/activity` — the closing `POSITION` activity's `source` (`SL`,
`TP`/`PROFIT`, `USER`). Order of precedence:
1. **activity source** → `SL` ⇒ sl_hit, `TP`/`PROFIT` ⇒ tp_hit, `USER` ⇒ manual
   (a source=SL that hit an SL ratcheted to ~entry is booked **break-even**);
2. else the **heuristic**: break-even if the SL was moved to ~entry and hit or
   realized P&L ≈ 0; otherwise tp_hit/sl_hit by close-price proximity, tie-broken
   by the sign of realized P&L.
- Realized **P&L** always comes from the matching `/history/transactions` row
  (signed, in account currency). `size` may arrive unsigned; a stop-out is booked
  as a loss.

### Full audit — `position_activities`
Every `/history/activity` item for a tracked deal (working order executed,
position opened, `EDIT_STOP_AND_LIMIT`, SL/TP/user close) is recorded once in the
**`position_activities`** table (`deal_id`, `deal_reference`, `source`, `type`,
`status`, `activity_at`, and realized P&L + currency for closes). This is the
broker-agnostic "truth" log — it makes SL-moved-to-entry, exact per-position
P&L, and the whole lifecycle queryable for later analysis, and is surfaced under
`GET /trades/{id}` as `activities`. `Leg` keeps its live `dealId` refs for
reconciliation; this table never changes if a broker's id scheme differs.

## Order type — LIMIT with per-leg MARKET fallback
Sources no longer choose MARKET vs LIMIT. Every entry rests as a **LIMIT** at its
signalled level, EXCEPT a leg whose entry the current candle has already crossed
(`build_plan` folds the latest 1-min candle high/low with the live price): that
leg opens **MARKET** now, because the price already touched the level and may not
rebound. Decided per entry level — a two-level entry can be one MARKET fill plus
one resting LIMIT. All already-crossed entries collapse into a single market fill
(never double the size), and a leg whose SL/TP is untradeable from the actual
fill is dropped.

## Key code
- `beacon_core/db/models.py` — `PositionActivity` (the audit/truth table).
- `beacon_core/brokers/types.py` — `BrokerPosition.working_order_ref`.
- `beacon_core/brokers/capital_com.py` — `list_positions` (surfaces
  `workingOrderId`), `get_transactions` (dealId + P&L-from-`size` mapping),
  `get_activity` (`/history/activity`), `place_order`.
- `beacon_core/execution/planner.py` — `build_plan` (LIMIT + per-leg MARKET
  fallback via candle cross).
- `services/monitor/main.py` — `_process_trade` reconciliation, `_txn_by_dealid`,
  `_close_source`/`_outcome_from_source`, `_audit_activities`, `_classify_outcome`.

## Known limitations / follow-ups
- The closing transaction has no close level, so the ledger close *price* is a
  live-price approximation; realized **P&L** is exact (from the broker).
- Fast fill+close of a working order (both inside one 5s tick, position id never
  captured) falls back to matching an unclaimed close on the same instrument —
  best-effort and logged.
- Heuristic P&L (used only when no broker transaction is found) is in the
  *instrument* currency, whereas broker P&L is in the *account* currency.
