# `parsing/` — signals & outcomes from free text

Turns the messy free text of a trading channel into typed objects. Intentionally
simple and built to iterate; it favors precision (drop ambiguous text) over
catching every exotic format.

| File | Purpose |
|------|---------|
| `models.py` | `ParsedSignal` — the typed result of parsing (symbol, direction, `entry_from/entry_to`, `sl`, `tps`, order-type hint, raw text). |
| `gold.py` | The signal parser. A four-part gate must all be present (symbol, direction, entry, SL) before it emits a `ParsedSignal`. Symbol-aware port of the original gold parser. |
| `symbols.py` | Symbol registry. A symbol's **price band** is what lets the parser tell a real price from a pip-count or a ratio. Gold is wired for Phase 1; adding Silver/FX is another `SymbolSpec`, no parser change. |
| `outcomes.py` | Parses channel follow-ups ("TP2 HIT", "SL HIT 80 PIPS", "all TP done ✅") into a structured outcome claim. Pure; returns `None` for non-outcome text so it can run cheaply over every message. |

**How it fits:** the ingest pipeline calls the parser; a valid `ParsedSignal`
becomes a `Signal` row. Outcome claims feed the reconciler (channel-claimed vs
bot-actual). The price-band idea in `symbols.py` is the core trick that makes the
parser robust without hardcoding formats.
