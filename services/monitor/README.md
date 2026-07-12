# `monitor/` — reconcile, protect, close

Loops every `MONITOR_INTERVAL` seconds. For each open trade it reconciles the DB
ledger against the broker's live state and manages the position to close. The
**broker is the source of truth**; the monitor keeps one logged-in adapter per
account (rebuilt only on auth failure).

## Each tick (`_process_trade`)
- **Fill detection** — a working order that left the book becomes a position
  (linked by `workingOrderId`, globally unique so two trades can't claim one
  position), was cancelled, or filled+closed inside a tick.
- **Close detection** — a position that vanished is closed; realized P&L and the
  close reason come from the broker's closing transaction/activity (matched by
  exact dealId; heuristic fallback emits a `reconcile_unmatched` event).
- **SL-move ratchet** — `strategy.evaluate()` off the **live price**; when it
  returns a tighter stop, issue `modify_position`. Capital protection never
  depends on fill-price heuristics.
- **Working-order TTL** — expire/cancel unfilled entries past `entry_ttl_minutes`
  (defense-in-depth on top of the broker-side `goodTillDate`).
- **Trade rollup** — sum leg P&L; mark `closed`/`partial`; on close run AI
  outcome analysis.

## Periodic
- **Weekly structure recompute** (`_maybe_recompute_structure`) — fires the
  versioned market-structure/magnet map rebuild in the background (own session),
  zero impact on the trade loop.

`main.py` is the whole service; the rules/estimators live in `beacon_core`.
