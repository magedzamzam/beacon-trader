# `ta/` — technical-analysis indicators

A config-driven indicator engine. The **registry is the single source of truth**:
add one entry (id, label, category, params, compute) and the indicator shows up
in the portal, is selectable per timeframe, and gets captured — no other file
changes.

| File | Purpose |
|------|---------|
| `registry.py` | `REGISTRY` (every indicator spec), `Ctx` (per-TF price window: opens/highs/lows/closes/volumes/price), `TF_RESOLUTION`, `DEFAULT_CONFIG`, and `compute_one()`. Includes structure indicators (S/R, Fibonacci, Fair Value Gap, Order Block). |
| `indicators.py` | The pure math: RSI, MACD, EMA/SMA/WMA, ATR, ADX, Bollinger/Keltner/Donchian, VWAP/OBV, swings, Fibonacci, and the FVG/Order-Block detectors (#59). |
| `features.py` | `compute_timeframe(bars, price, indicators)` — assemble one timeframe's indicator snapshot from the configured list. |
| `capture.py` | `capture_for_signal()` — fetch bars per configured timeframe, compute the indicators, and upsert one `signal_features` row. **Off the hot path** (runs in the background after orders are placed) and best-effort. Also invokes the analytics sidecar reusing the same bars. |

**How it fits:** each signal gets a multi-timeframe TA snapshot persisted to
`signal_features`, so trade outcomes can later be correlated with the conditions
the signal fired under (via `analysis/`). Nothing here gates trading; capture
adds zero execution latency.
