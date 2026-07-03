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

### Outcome classification
- **break-even** if EITHER the SL was ratcheted to ~entry and hit, OR realized
  P&L ≈ 0 (`|pl| <= BE_MONEY_TOL`, account currency).
- else **tp_hit** / **sl_hit** by the close price's proximity to TP/SL, with the
  sign of realized P&L as the tie-breaker (a position auto-closes at its attached
  profitLevel/stopLevel, so profit ⇒ TP, loss ⇒ SL).
- `size` can arrive unsigned; a stop-out is always booked as a loss.

## Key code
- `beacon_core/brokers/types.py` — `BrokerPosition.working_order_ref`.
- `beacon_core/brokers/capital_com.py` — `list_positions` (surfaces
  `workingOrderId`), `get_transactions` (dealId + P&L-from-`size` mapping),
  `place_order` (working order → confirm dealId; MARKET → affectedDeals dealId).
- `services/monitor/main.py` — `_process_trade` reconciliation, `_txn_by_dealid`,
  `_classify_outcome`.

## Known limitations / follow-ups
- The closing transaction has no close level, so the ledger close *price* is a
  live-price approximation; realized **P&L** is exact (from the broker).
- Fast fill+close of a working order (both inside one 5s tick, position id never
  captured) falls back to matching an unclaimed close on the same instrument —
  best-effort and logged.
- Heuristic P&L (used only when no broker transaction is found) is in the
  *instrument* currency, whereas broker P&L is in the *account* currency.
