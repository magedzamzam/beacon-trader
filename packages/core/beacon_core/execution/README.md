# `execution/` — plan, guard, and (optional) filter

The pure decision logic the `executor` runs before it places orders. No broker
or DB calls live here — these are functions of the parsed signal + config.

| File | Purpose |
|------|---------|
| `planner.py` | `validate_signal()` and `build_plan()` — turn a `ParsedSignal` into the **fanout**: one leg per (entry × take-profit). A range entry with 3 TPs → 6 legs. Also decides MARKET vs LIMIT per leg and drops legs whose TP violates broker minimums. |
| `guard.py` | Two capital-protection guards: `should_auto_execute()` (source must be enabled + trusted + not name-blocklisted) and `risk_limit_reason()` (kill-switch + daily-loss floor **always** enforced; per-signal/open-risk caps opt-in). Plus `DEFAULT_RISK_LIMITS`. |
| `trend_filter.py` | Trend-alignment entry filter (#48): skip or de-size a signal that fights the higher-TF EMA200 trend. Config-driven, clamped, fail-open. This one **is** wired into the executor (an accepted entry filter), unlike the shadow filters in `analysis/`. |

**How it fits:** the executor calls `should_auto_execute` → `build_plan` →
`risk/sizing` → `risk_limit_reason` → (optional AI review) → broker. The guard's
kill-switch and daily-loss floor are enforced unconditionally so a mis-set
`enabled: false` cannot silently disarm capital protection.
