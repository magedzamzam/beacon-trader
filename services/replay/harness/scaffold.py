"""Emit a run config that reproduces the LIVE setup (#169 §5).

The validation gate says "replay the ACTUAL live configs over the ACTUAL
historical signals". Hand-transcribing `execution_strategies`,
`account_source_risk`, the `risk_limits` setting and the symbol map into JSON is
exactly the kind of work that produces a config which is *nearly* live — and a
gate run against a nearly-live config measures the transcription, not the
simulator. So the harness reads them and writes the JSON itself.

The output is an ordinary variant, editable like any other. That is the point:
the live config becomes the BASELINE arm of a sweep, and a counterfactual is
whatever you change from it.

PURE — the assembly is stdlib only; `store.load_live_config` does the SELECTs.

WHAT CANNOT BE READ, and is therefore surfaced rather than guessed:

  equity      lives at the broker, not in the ledger. Sizing is only comparable
              to live if the budget is, so it is a required input.
  fx_factor   computed live from a broker FX quote, per trade. Constant here;
              1 is correct only when the account and instrument currencies
              match — which for an `AEDd` account they do NOT.

Both land in the config as real values with a `_needs_review` list naming them,
so the gap is in front of whoever runs the gate rather than behind them.
"""
from __future__ import annotations

from typing import Optional

# The live subsystems the simulator does not model. Named in the emitted config
# because they are the honest reasons a validation run can disagree with reality
# for a cause that is not a bug in the fill logic.
UNMODELLED = [
    "session risk multiplier (trading_hours.sessions[].risk_mult) — needs a clock",
    "counter-trend de-size (execution/trend_filter) — needs broker bars at entry",
    "correlation-cluster budgeter (risk/cluster) — shadow live, not simulated",
    "AI pre-trade review/gate — needs a provider call",
    "confirm-404 rejects (#150), orphaned armed STOPs (#161), fill_price=0 (#159)"
    " — broker faults with no candle signature",
]


def build_run_config(*, accounts, sources, strategies, account_source_risk,
                     risk_limits, symbol_map, equity, symbol="XAUUSD",
                     frm=None, to=None, holdout_from=None,
                     label="live config (validation baseline)") -> dict:
    """Assemble the run config. Every argument is a plain list/dict of rows, so
    this is testable without a database."""
    acct_rows = []
    for a in accounts:
        acct_rows.append({
            "id": a["id"], "name": a.get("name") or f"acct#{a['id']}",
            "equity": _equity_for(equity, a["id"]),
            "currency": a.get("currency") or "USD",
            "fx_factor": 1,
        })

    by_account = {str(a["id"]): dict(a.get("risk_config") or {})
                  for a in accounts if a.get("risk_config")}
    by_pair = {f"{r['account_id']}:{r['source_id']}": dict(r.get("risk_config") or {})
               for r in account_source_risk if r.get("enabled", True)
               and r.get("risk_config")}

    variant = {
        "name": "live",
        "accounts": acct_rows,
        # Emitted in the SAME scope order the cascade resolves, most-specific
        # first, so the JSON reads the way `resolve_chain` behaves.
        "strategies": [_strategy(s) for s in _ordered(strategies)],
        "risk": {"default": {}, "by_account": by_account,
                 "by_account_source": by_pair},
        "risk_limits": dict(risk_limits or {}),
        "instrument": dict(symbol_map or {}),
        "costs": {"slippage_points": 0.0},
        "horizon_bars": 1440,
        "ratchet_price": "extreme",
    }

    needs_review = ["accounts[].equity — read from the broker, not the ledger; "
                    "set it to the real demo equity or sizing is not comparable"]
    if any(str(a.get("currency") or "").upper() not in ("USD", "")
           for a in accounts):
        needs_review.append(
            "accounts[].fx_factor — left at 1, but an account whose currency is "
            "not the instrument's needs the real account->instrument factor, or "
            "every lot is wrong by that ratio")
    if not risk_limits:
        needs_review.append(
            "risk_limits — no `risk_limits` setting found; the harness will "
            "apply DEFAULT_RISK_LIMITS, which is NOT what live is running")
    if not symbol_map:
        needs_review.append(
            f"instrument — no symbol_map for {symbol}; value_per_point defaults "
            "to 1 and every lot will be wrong")

    return {
        "label": label,
        "symbol": symbol,
        "timeframe": "1m",
        "from": frm, "to": to, "holdout_from": holdout_from,
        "signal_source": "historical",
        "workers": 0,
        "variants": [variant],
        "_generated": {
            "by": "main.py scaffold",
            "purpose": ("Reproduces the live config for the §5 validation gate. "
                        "Also the baseline arm of any sweep — a counterfactual "
                        "is whatever you change from this."),
            "_needs_review": needs_review,
            "_not_modelled": UNMODELLED,
            "n_accounts": len(acct_rows),
            "n_strategies": len(variant["strategies"]),
            "n_source_risk_overrides": len(by_pair),
        },
    }


def _equity_for(equity, account_id) -> float:
    """A single number for every account, or a per-account mapping."""
    if isinstance(equity, dict):
        return equity.get(str(account_id), equity.get(account_id, 0))
    return equity


def _ordered(strategies) -> list:
    """Most-specific scope first — (acct,src) > (acct,*) > (*,src) > (*,*).
    Cosmetic (`resolve_chain` sorts for itself), but a config a human is going
    to edit should read in the order it resolves."""
    def key(s):
        spec = (2 if s.get("account_id") is not None else 0) + \
               (1 if s.get("source_id") is not None else 0)
        return (-spec, s.get("account_id") or 0, s.get("source_id") or 0)
    return sorted(strategies, key=key)


def _strategy(s) -> dict:
    """One `execution_strategies` row as a variant strategy. Pillars are copied
    verbatim — including a `staged` block — because the point is fidelity, and a
    normalised copy is a different config."""
    out = {"account_id": s.get("account_id"), "source_id": s.get("source_id"),
           "enabled": bool(s.get("enabled", True)),
           "label": s.get("label") or _auto_label(s)}
    for pillar in ("entry_policy", "entry_filters", "exit_policy"):
        val = s.get(pillar)
        if val:
            out[pillar] = dict(val)
    return out


def _auto_label(s) -> str:
    a, src = s.get("account_id"), s.get("source_id")
    return f"acct{a if a is not None else '*'}/src{src if src is not None else '*'}"


def summarise(cfg: dict) -> dict:
    """The one-screen digest printed after writing the file — what was read, and
    what still needs a human."""
    gen = cfg.get("_generated") or {}
    v = (cfg.get("variants") or [{}])[0]
    return {
        "accounts": [{"id": a["id"], "name": a["name"], "equity": a["equity"],
                      "currency": a["currency"]} for a in v.get("accounts", [])],
        "strategies": [{"scope": f"acct={s.get('account_id')} src={s.get('source_id')}",
                        "label": s.get("label"),
                        "pillars": sorted(k for k in
                                          ("entry_policy", "entry_filters", "exit_policy")
                                          if s.get(k))}
                       for s in v.get("strategies", [])],
        "risk_limits_keys": sorted((v.get("risk_limits") or {}).keys()),
        "instrument": v.get("instrument"),
        "needs_review": gen.get("_needs_review"),
        "not_modelled": gen.get("_not_modelled"),
    }
