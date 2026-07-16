"""Position sizing. Two things are configurable and independent:

  basis       how the per-signal budget is set
              - "capital_percent": budget = equity * value/100
              - "fixed_cash":      budget = value   (exact $ you'll risk)

  allocation  how that budget is spread across legs
              - "even":    each leg risks budget / N
              - "per_tp":  each leg risks equity * per_tp_percent[tp_index]/100
                           (mirrors tpN_capital_risk_percent from the old bot)

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

    for leg in active:
        if risk.allocation == "per_tp" and risk.per_tp_percent:
            pct = risk.per_tp_percent.get(leg.tp_index)
            if pct is None:
                pct = min(risk.per_tp_percent.values())  # unlisted TP: smallest
            risk_cash = equity * pct / Decimal(100)
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
