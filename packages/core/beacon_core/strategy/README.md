# `strategy/` — stop-loss ratchet engine

| File | Purpose |
|------|---------|
| `rules.py` | `PositionCtx`, `evaluate()`, and `DEFAULT_SL_RULES` — the declarative SL-move rule engine the monitor runs off the **live price**. |

## How it works
Rules are declared **per source** (`strategy.sl_rules`) and evaluated every
monitor tick. They **chain** — each fires independently and the engine applies
whichever tightens the stop most; it **never loosens** an existing stop.

- **Triggers:** `tp_hit` (a TP index has been reached) or `price_move` (points).
- **Actions** (`move_sl_to`): `entry`, `number` (a value), `tp` (index),
  `previous_tp`.

The classic ratchet (`DEFAULT_SL_RULES`):
```
TP1 hit → SL to entry
TP2 hit → SL to previous_tp   (TP1)
TP3 hit → SL to previous_tp   (TP2)
```

**Why it matters:** SL-move decisions run purely off live price and the rule
config — they do **not** depend on exact fill/close correlation, so capital
protection is unaffected by REST heuristics. The monitor calls `evaluate()` and,
when it returns a tighter stop, issues a broker `modify_position`.
