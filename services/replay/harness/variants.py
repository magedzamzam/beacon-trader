"""Config-as-data: a replay variant is a full execution config, not a code path.

A run is `{signal_selector, date_range, config_variant}`, and a sweep is N
variants. Adding an experiment must never mean editing the simulator — that is
the difference between "N questions per afternoon" and "one branch per question"
(#169 §4).

A `config_variant` is shaped like the live tables it stands in for:

    {
      "name": "be_at_tp2",
      "accounts":  [{"id": 1, "name": "A", "equity": 10000, "currency": "AEDd"}],
      "strategies": [                       # execution_strategies rows
        {"account_id": null, "source_id": null,
         "entry_policy":  {"entry_style": "limit", "ttl_minutes": 60},
         "entry_filters": {"rules": [...]},
         "exit_policy":   {"sl_rules": [...], "cancel_pending_on_stop": true}},
        {"account_id": 1, "source_id": 7,    # PER-CHANNEL override (§6)
         "exit_policy":   {"sl_rules": [ ... BE@TP2 ... ]}}
      ],
      "risk":        {"default": {...}, "by_account": {...},
                      "by_account_source": {"1:7": {...}}},
      "risk_limits": {...},                 # the `risk_limits` SETTING shape
      "instrument":  {"value_per_point": 1, "min_lot": 0.01, ...},
      "costs":       {"slippage_points": 0.0},
      "horizon_bars": 1440
    }

`strategies` is resolved by the REAL `execution.strategy.resolve_chain` /
`entry_policy` / `resolve_entry_filters` / `exit_sl_rules`, so the scope cascade
((acct,src) > (acct,*) > (*,src) > (*,*)) and the pillar inheritance are the
shipped ones. That is what makes "BE@TP2 for TFXC but BE@TP1 for Yulia" ONE
variant rather than two runs — and it means a cascade bug in live is a cascade
bug in replay, which is the point.

PURE — stdlib + beacon_core's pure engines only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import DEFAULT_PLANNER
from beacon_core.execution.guard import DEFAULT_RISK_LIMITS
from beacon_core.risk.sizing import InstrumentSpec, RiskConfig, resolve_risk_config
from beacon_core.trading_hours import sessions as TH

# Equity is held CONSTANT across a run rather than compounded. Two reasons, both
# about comparability: R-multiples are the headline metric and compounding makes
# them path-dependent, and a variant that happens to win early would otherwise
# size up and beat an identical variant that won late. State it, don't hide it.
DEFAULT_EQUITY = Decimal("10000")


class StrategyRow:
    """Duck-typed stand-in for `db.models.ExecutionStrategy`. `resolve_chain` and
    the pillar getters read their inputs with `getattr`, so a plain object with
    the same attribute names resolves identically to an ORM row — no DB, and no
    second implementation of the cascade to keep in sync."""

    __slots__ = ("account_id", "source_id", "entry_policy", "entry_filters",
                 "exit_policy", "enabled", "label", "id")

    def __init__(self, d: dict):
        d = d or {}
        self.id = d.get("id")
        self.account_id = d.get("account_id")
        self.source_id = d.get("source_id")
        self.entry_policy = d.get("entry_policy") or {}
        self.entry_filters = d.get("entry_filters") or {}
        self.exit_policy = d.get("exit_policy") or {}
        self.enabled = bool(d.get("enabled", True))
        self.label = d.get("label")


@dataclass(frozen=True)
class AccountSpec:
    id: int
    name: str = ""
    equity: Decimal = DEFAULT_EQUITY
    currency: str = "USD"
    # account -> instrument currency conversion, exactly as `size_legs` uses it.
    # 1 when they match. Constant per run: an FX series is a second data-quality
    # problem the harness has no way to validate, so it is an explicit input.
    fx_factor: Decimal = Decimal("1")


@dataclass(frozen=True)
class ResolvedConfig:
    """Everything one (account, source) pair needs to simulate a signal. Built
    once per pair per variant and cached — resolving the cascade per signal over
    800 signals x N variants is pure waste."""
    entry_policy: Dict[str, Any]
    filter_rules: List[dict]
    sl_rules: List[dict]
    sl_rules_origin: str
    cancel_pending_on_stop: bool
    risk: RiskConfig
    ttl_minutes: int


@dataclass
class Variant:
    name: str
    strategies: List[StrategyRow] = field(default_factory=list)
    accounts: List[AccountSpec] = field(default_factory=list)
    risk: Dict[str, Any] = field(default_factory=dict)
    risk_limits: Dict[str, Any] = field(default_factory=dict)
    instrument: InstrumentSpec = field(
        default_factory=lambda: InstrumentSpec(value_per_point=Decimal("1")))
    min_stop_distance: Optional[Decimal] = None
    costs: Dict[str, Any] = field(default_factory=dict)
    # Session windows (#81), in the shape of the `trading_hours` SETTING.
    # Present  -> the session risk multiplier AND `session_in` filter rules are
    #             MODELLED, using the shipped `trading_hours.sessions` functions.
    # Absent   -> neither is, and the report says so rather than leaving it to be
    #             discovered from a divergence.
    #
    # Absent is the DEFAULT on purpose. Defaulting to DEFAULT_SESSIONS would be
    # guessing this install's config, and would silently change the results of
    # every run config written before this shipped — the one thing a
    # reproducibility claim cannot survive. `scaffold` reads the real setting, so
    # the validation baseline gets it without anyone needing to know it exists.
    trading_hours: Optional[Dict[str, Any]] = None
    horizon_bars: int = 1440
    # Which price a ratchet trigger reads inside a bar. "extreme" is the closest
    # match to a monitor polling many times a minute (and to the MFE latching in
    # #149/#160); "close" reproduces a once-per-bar poll. Stated, configurable,
    # and reported — not silently one or the other.
    ratchet_price: str = "extreme"
    # WHEN a ratchet takes effect within the bar (#185, defect B).
    #   "next_bar" (default) — the stop moves after this bar's exits are
    #       resolved, so it protects the position only from the NEXT bar.
    #       Conservative about the STOP, and optimistic about the OUTCOME: it
    #       skips breakevens live actually took, which is 21 of the validation
    #       gate's 72 disagreements and the largest single contributor to the
    #       residual +0.060R.
    #   "same_bar" — the stop moves on this bar's favourable extreme and is then
    #       tested against this bar's adverse extreme: a monitor that ratcheted
    #       mid-minute and was taken out by the retrace inside the same minute.
    # Neither is obviously right, so it is a stated variant key to be settled by
    # re-running the §5 gate both ways — not a constant someone chose once.
    ratchet_timing: str = "next_bar"
    raw: Dict[str, Any] = field(default_factory=dict)
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    # --- resolution -----------------------------------------------------------
    def resolve(self, account_id, source_id) -> ResolvedConfig:
        key = (account_id, source_id)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        chain = ST.resolve_chain(self.strategies, account_id, source_id)
        ep = ST.entry_policy(chain, global_planner=DEFAULT_PLANNER)
        filters = ST.resolve_entry_filters(chain)
        sl_rules, origin = ST.exit_sl_rules(chain)
        cps = ST.cancel_pending_on_stop(chain)
        risk = RiskConfig.from_dict(resolve_risk_config(
            self._risk_override(account_id, source_id), True,
            self._account_risk(account_id)))
        out = ResolvedConfig(
            entry_policy=ep, filter_rules=list((filters or {}).get("rules") or []),
            sl_rules=sl_rules, sl_rules_origin=origin, cancel_pending_on_stop=cps,
            risk=risk, ttl_minutes=_ttl(ep))
        self._cache[key] = out
        return out

    def _risk_override(self, account_id, source_id):
        by_pair = (self.risk or {}).get("by_account_source") or {}
        return by_pair.get(f"{account_id}:{source_id}")

    def _account_risk(self, account_id):
        by_acct = (self.risk or {}).get("by_account") or {}
        return (by_acct.get(str(account_id)) or by_acct.get(account_id)
                or (self.risk or {}).get("default") or {})

    def account(self, account_id) -> Optional[AccountSpec]:
        for a in self.accounts:
            if a.id == account_id:
                return a
        return None

    @property
    def slippage_points(self) -> float:
        try:
            return float((self.costs or {}).get("slippage_points") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def session_windows(self) -> Optional[list]:
        """The configured session list, or None when sessions are not modelled."""
        th = self.trading_hours
        if not isinstance(th, dict):
            return None
        return th.get("sessions") or None

    def session_context(self, when) -> tuple:
        """`(active_session_labels, risk_multiplier)` at `when`.

        Both come from `trading_hours.sessions` — the SAME pure functions the
        executor reaches through `th_service` — so a replayed London/NY overlap
        de-sizes by exactly the factor live would apply. `([], 1.0)` when
        sessions are not configured for this variant, which is also the
        fail-open the executor takes when the lookup errors."""
        windows = self.session_windows
        if not windows or when is None:
            return [], 1.0
        try:
            st = TH.status(windows, when)
            return list(st.get("active") or []), float(st.get("risk_multiplier", 1.0))
        except Exception:
            return [], 1.0                       # fail-open, exactly as live

    def digest(self) -> str:
        """Content hash of the variant as authored. Two runs with the same digest
        MUST produce identical results; a changed digest is the honest reason a
        re-run differs. Recorded on `replay_runs` alongside the git SHA."""
        return canonical_digest(self.raw)


def _ttl(entry_policy: dict) -> int:
    """The working-order TTL in minutes, via the SAME clamp the executor uses, so
    a variant cannot express a TTL live would refuse."""
    from beacon_core.config import effective_entry_ttl_min
    return effective_entry_ttl_min({"entry_ttl_minutes": entry_policy.get("ttl_minutes")})


def _dec(v, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(default)


def build_variant(d: dict) -> Variant:
    """Materialise a variant from its JSON form. Unknown keys are kept in `raw`
    (and therefore in the digest) but never interpreted — a typo must not
    silently become a default."""
    d = d or {}
    inst = d.get("instrument") or {}
    accounts = [AccountSpec(
        id=int(a["id"]), name=str(a.get("name") or ""),
        equity=_dec(a.get("equity", DEFAULT_EQUITY), str(DEFAULT_EQUITY)),
        currency=str(a.get("currency") or "USD"),
        fx_factor=_dec(a.get("fx_factor", 1), "1") or Decimal("1"),
    ) for a in (d.get("accounts") or [])]
    msd = inst.get("min_stop_distance")
    return Variant(
        name=str(d.get("name") or "unnamed"),
        strategies=[StrategyRow(s) for s in (d.get("strategies") or [])],
        accounts=accounts,
        risk=d.get("risk") or {},
        # A MISSING risk_limits block is the fail-safe case, exactly as in the
        # executor: an unconfigured install trades with the conservative defaults,
        # so a variant that forgets the block does not get an uncapped backtest.
        risk_limits=dict(d.get("risk_limits") or DEFAULT_RISK_LIMITS),
        instrument=InstrumentSpec(
            value_per_point=_dec(inst.get("value_per_point", 1), "1"),
            min_lot=_dec(inst.get("min_lot", "0.01"), "0.01"),
            lot_step=_dec(inst.get("lot_step", "0.01"), "0.01")),
        min_stop_distance=None if msd in (None, "") else _dec(msd),
        costs=d.get("costs") or {},
        trading_hours=d.get("trading_hours") or None,
        horizon_bars=int(d.get("horizon_bars") or 1440),
        ratchet_price=str(d.get("ratchet_price") or "extreme"),
        ratchet_timing=_ratchet_timing(d.get("ratchet_timing")),
        raw=d,
    )


RATCHET_NEXT_BAR = "next_bar"
RATCHET_SAME_BAR = "same_bar"
RATCHET_TIMINGS = (RATCHET_NEXT_BAR, RATCHET_SAME_BAR)


def _ratchet_timing(v) -> str:
    """An unrecognised value falls back to today's behaviour rather than being
    accepted silently. A typo that quietly selected the OTHER exit model would
    make two runs incomparable with nothing on the result to say why."""
    s = str(v or "").strip().lower()
    return s if s in RATCHET_TIMINGS else RATCHET_NEXT_BAR


def canonical_digest(obj) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace). The reproducibility
    primitive: same inputs -> same digest -> same results, checkable without
    re-running."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
