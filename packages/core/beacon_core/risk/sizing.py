"""Position sizing. Two things are configurable and independent:

  basis       how the per-signal budget is set
              - "capital_percent": budget = equity * value/100
              - "fixed_cash":      budget = value   (exact $ you'll risk)

  allocation  how that budget is spread across legs
              - "even":    each leg risks budget / N
              - "per_tp":  each leg risks equity * per_tp_percent[tp_index]/100
                           (mirrors tpN_capital_risk_percent from the old bot)

`per_tp` and ZONE signals (#154): the single-shot planner fans a zone entry onto
BOTH edges, so TWO legs carry the same tp_index and each takes the full per-TP
percent — the signal's intended total is doubled. The confirmation-staged planner
emits one leg per tp_index, so it is NOT doubled, and an A-vs-C comparison of the
two entry styles is then comparing different stakes. `per_tp_split_across_entries`
divides a tp_index's percent across the legs sharing it, making "risk X% on TP1"
mean X% total. It defaults to FALSE — today's behaviour — because turning it on
HALVES live risk on any account using `per_tp` with zone signals.


lot = risk_cash / (|entry - sl| * value_per_point)

value_per_point is money per 1.0 price move per 1.0 broker size, and MUST be
calibrated per broker instrument (stored on the symbol map). It is the one
number that makes real-money sizing correct, so it is explicit, never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional

from ..execution.planner import PlannedLeg


@dataclass
class RiskConfig:
    basis: str = "capital_percent"          # capital_percent | fixed_cash
    value: Decimal = Decimal("1.0")          # percent, or cash
    allocation: str = "even"                 # even | per_tp
    per_tp_percent: Dict[int, Decimal] = None  # {1: 4.0, 2: 2.0, ...}
    # per_tp only (#154): divide a tp_index's percent across the legs sharing it
    # (a zone signal fans onto both edges) so the per-TP percent is the TOTAL for
    # that TP, not per-leg. False = today's behaviour; see the module docstring.
    per_tp_split_across_entries: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        d = d or {}
        raw = d.get("per_tp_percent") or {}
        per_tp = {int(k): Decimal(str(v)) for k, v in raw.items()}
        return cls(
            basis=d.get("basis", "capital_percent"),
            value=Decimal(str(d.get("value", "1.0"))),
            allocation=d.get("allocation", "even"),
            per_tp_percent=per_tp or None,
            per_tp_split_across_entries=bool(d.get("per_tp_split_across_entries", False)),
        )


def resolve_risk_config(override_cfg, override_enabled, account_cfg) -> dict:
    """Effective RiskConfig dict for a trade (#84): the per-(account,source) risk
    override wins when enabled AND non-empty, else the account's overall risk_config,
    else {} (RiskConfig.from_dict then applies conservative defaults). Sources no
    longer carry risk — it all resolves here from Risk & Limits config."""
    if override_enabled and override_cfg:
        return dict(override_cfg)
    return dict(account_cfg or {})


@dataclass
class InstrumentSpec:
    value_per_point: Decimal            # money per 1.0 price move per 1.0 size
    min_lot: Decimal = Decimal("0.01")
    lot_step: Decimal = Decimal("0.01")


def _round_lot(lot: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return lot
    return (lot / step).to_integral_value(rounding=ROUND_DOWN) * step


def size_legs(legs: List[PlannedLeg], *, equity: Decimal, risk: RiskConfig,
              instrument: InstrumentSpec, fx_factor: Decimal = Decimal(1)) -> List[PlannedLeg]:
    """Fill lot / risk_cash on each valid leg. Legs whose lot rounds below
    min_lot are marked invalid (dropped) rather than silently over-risked.

    equity and the risk budget are in ACCOUNT currency; value_per_point is in
    the INSTRUMENT's currency. fx_factor converts account -> instrument currency
    (1 when they match), so lots come out correct for USD or non-USD accounts.
    """
    active = [l for l in legs if l.valid]
    n = len(active) or 1

    if risk.basis == "fixed_cash":
        budget = risk.value
    else:  # capital_percent
        budget = (equity * risk.value / Decimal(100))

    # How many legs share each tp_index (#154). A zone signal fans onto both edges,
    # so a per_tp allocation would otherwise charge the TP's percent once per leg.
    per_index_count: Dict[int, int] = {}
    if risk.allocation == "per_tp" and risk.per_tp_split_across_entries:
        for l in active:
            per_index_count[l.tp_index] = per_index_count.get(l.tp_index, 0) + 1

    for leg in active:
        if risk.allocation == "per_tp" and risk.per_tp_percent:
            pct = risk.per_tp_percent.get(leg.tp_index)
            if pct is None:
                pct = min(risk.per_tp_percent.values())  # unlisted TP: smallest
            risk_cash = equity * pct / Decimal(100)
            share = per_index_count.get(leg.tp_index, 1)
            if share > 1:
                risk_cash = risk_cash / Decimal(share)
        else:
            risk_cash = budget / Decimal(n)

        # Convert the account-currency risk into instrument-currency terms.
        risk_cash_instr = risk_cash * fx_factor

        distance = abs(leg.entry - leg.sl)
        if distance <= 0:
            leg.valid = False
            leg.skip_reason = "zero entry/SL distance"
            continue

        raw_lot = risk_cash_instr / (distance * instrument.value_per_point)
        lot = _round_lot(raw_lot, instrument.lot_step)
        if lot < instrument.min_lot:
            leg.valid = False
            leg.skip_reason = f"lot {lot} below min {instrument.min_lot}"
            continue
        leg.lot = lot
        # Store risk back in ACCOUNT currency for reporting consistency.
        leg.risk_cash = (lot * distance * instrument.value_per_point) / fx_factor
    return legs


def plan_total_risk(legs: List[PlannedLeg]) -> Decimal:
    """Worst-case cash lost if the shared SL takes every open leg."""
    return sum((l.risk_cash for l in legs if l.valid and l.risk_cash), Decimal(0))


def cap_total_risk(legs: List[PlannedLeg], *, cap: Decimal, instrument: InstrumentSpec,
                   fx_factor: Decimal = Decimal(1)) -> Decimal:
    """Per-signal risk cap (#78): scale every valid leg's lot down proportionally
    so the summed worst-case-to-SL risk does not exceed `cap` (account currency).

    A multi-entry × multi-TP `per_tp` fanout risks each leg independently, so its
    aggregate can be several × the intended single-unit risk. This bounds it. Legs
    that fall below min_lot after scaling are dropped (invalidated). No-op when the
    plan is already under the cap, or cap <= 0 (disabled). Returns the new total —
    always <= the original. Only ever REDUCES exposure (never sizes up)."""
    total = plan_total_risk(legs)
    if cap <= 0 or total <= 0 or total <= cap:
        return total
    scale = cap / total
    for leg in legs:
        if not (leg.valid and leg.lot):
            continue
        lot = _round_lot(leg.lot * scale, instrument.lot_step)
        if lot < instrument.min_lot:
            leg.valid = False
            leg.skip_reason = "risk-cap scaled below min lot"
            continue
        leg.lot = lot
        distance = abs(leg.entry - leg.sl)
        leg.risk_cash = (lot * distance * instrument.value_per_point) / fx_factor
    return plan_total_risk(legs)


def deployed_risk(fills, *, original_sl: Decimal, instrument: InstrumentSpec,
                  fx_factor: Decimal = Decimal(1)) -> Decimal:
    """Risk ACTUALLY put on at the fills, in ACCOUNT currency (#188).

    `planned_risk` is what sizing intended to lose at the original stop. An arm
    that does not deploy its plan — which is exactly what `entry_style="staged"`
    does by construction — gets a mechanically better `R = pl / planned_risk` in
    a losing week without any entry-selection skill. Measuring the deployed side
    is what lets the two be told apart; without it, a 0.267x de-lever reads as a
    winning arm and, under the compounding rule, promotes permanently.

    Deliberately the SAME arithmetic as `size_legs` writes into `risk_cash`
    (`lot * distance * value_per_point / fx_factor`), so `deployed / planned` is
    a ratio of like for like rather than of two different units.

    Measured to the ORIGINAL stop, which the caller must supply: `legs.sl` is
    mutated in place by the ratchet engine, so a trade that ratcheted to
    breakeven would otherwise report a deployed risk near zero and flatter the
    ratio exactly where it matters most.

    `fills` is an iterable of `(lot, fill_price)`. Unfilled legs contribute
    nothing — an entry that never filled put no risk on."""
    total = Decimal(0)
    for lot, fill_price in fills:
        if lot is None or fill_price is None:
            continue
        lot, fill_price = Decimal(str(lot)), Decimal(str(fill_price))
        if lot <= 0 or fill_price <= 0:
            continue                       # fill_price 0 is UNKNOWN, not free (#159)
        distance = abs(fill_price - Decimal(str(original_sl)))
        total += (lot * distance * instrument.value_per_point) / (fx_factor or Decimal(1))
    return total


def risk_units(legs) -> Decimal:
    """Sum of `lot * |price - stop|` over `(lot, price, stop)` triples.

    The instrument-and-currency-free half of a risk figure: everything else in
    `risk_cash` is the constant `value_per_point / fx_factor`."""
    total = Decimal(0)
    for lot, price, stop in legs:
        if lot is None or price is None or stop is None:
            continue
        lot, price, stop = (Decimal(str(lot)), Decimal(str(price)), Decimal(str(stop)))
        if lot <= 0 or price <= 0:
            continue                       # a 0 fill_price is UNKNOWN, not free (#159)
        total += lot * abs(price - stop)
    return total


def deployed_risk_from_planned(*, planned_risk, planned_legs, filled_legs) -> Decimal | None:
    """Deployed risk in ACCOUNT currency, derived from the persisted plan (#188).

    `deployed = planned * (deployed_units / planned_units)`.

    Both sides carry the same `value_per_point / fx_factor`, so it CANCELS — which
    is the point. The monitor can therefore record deployed risk without an FX
    lookup, and an FX lookup on a per-tick loop is exactly the kind of broker call
    that should not exist on the path that manages open positions. It is also
    exact rather than approximate: the constants really are identical, because
    `planned_risk` was computed with them.

    `planned_legs` / `filled_legs` are `(lot, price, stop)` triples — the stop
    being the ORIGINAL one on both sides, since `legs.sl` is ratcheted in place
    and using the moved stop would report a de-levered trade as risking nothing.

    Returns None when it cannot be measured (no plan, no fills, zero planned
    units). None means "not measured" and is excluded downstream — never zero,
    which would read as "deployed nothing" and flatter the ratio."""
    if planned_risk is None:
        return None
    planned_units = risk_units(planned_legs)
    if planned_units <= 0:
        return None
    return (Decimal(str(planned_risk)) * risk_units(filled_legs)) / planned_units
