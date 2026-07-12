# `risk/` — position sizing

| File | Purpose |
|------|---------|
| `sizing.py` | `RiskConfig`, `InstrumentSpec`, and `size_legs()` — fill `lot` / `risk_cash` on each valid leg. |

## The model
Risk is **two independent choices**:

- **basis** — the per-signal budget:
  - `capital_percent` → budget = equity × value / 100
  - `fixed_cash` → an exact cash amount
- **allocation** — how the budget spreads across legs:
  - `even` → each leg risks budget / N
  - `per_tp` → each leg risks `equity × per_tp_percent[tp_index] / 100`

Then:
```
lot = risk_cash / (|entry − sl| × value_per_point × fx_factor)   # rounded down to lot_step
```
Legs whose lot rounds below `min_lot` are dropped (marked invalid) rather than
silently over-risked. `value_per_point` is in the **instrument** currency and
`fx_factor` converts the account-currency budget to it — so a USD or an AED
account both size correctly (the executor resolves `fx_factor` from `brokers/fx`).

**Calibrate `value_per_point`** on the symbol map before trading real funds — it
is the single most important sizing input.
