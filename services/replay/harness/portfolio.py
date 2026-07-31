"""Portfolio-level replay: admission, risk caps, the daily-loss breaker (#169 §5).

WHY THE JOB ATOM IS NOT `(signal x variant)`
--------------------------------------------
The issue proposes `(signal_id x variant)` as the unit of work. That atom cannot
model what §5 says the harness MUST model. `max_open_risk_per_symbol` and the
daily-loss breaker are PORTFOLIO state: whether signal 412 is taken depends on
what 405 and 409 already have open, and on what the account lost earlier that
day. The 2026-07-27 week is the proof — the 10k open-risk cap silently
determined which signals each account took, and a per-signal atom would score
every one of them as taken and overstate every variant.

So the atom here is `(variant x account)`: one deterministic portfolio replay.
Scale-out is unaffected — a sweep of N variants is N independent jobs with no
shared state, which is what "stateless workers, shards later with no rewrite"
actually needs. Signals within a variant are inherently sequential because the
caps make them so; pretending otherwise would be parallelism bought by
simulating a different bot.

COUNTERFACTUAL COVERAGE
-----------------------
Every signal is replayed from its STATED entry, so a signal we skipped,
filtered, risk-blocked or never filled is still evaluated (§6). Nothing is
dropped silently: each signal ends in exactly one bucket — `taken`, or a named
`not_taken` reason — and the buckets are reported. "How many did this variant's
caps block?" is then a count, not an inference.

PURE — stdlib + beacon_core. The DB lives in `store.py`.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from beacon_core.execution.guard import risk_limit_reason, soft_breaker_decision
from beacon_core.execution import strategy as ST
from beacon_core.parsing.models import ParsedSignal

from . import bars as B
from . import sim
from .context import ContextBuilder, MarketContext, filter_ctx
from .variants import Variant


@dataclass(frozen=True)
class SignalRow:
    """One signal to replay, source-agnostic. This is the `signal_source` seam:
    `historical` builds these from the `signals` table, a Phase-2 generator builds
    them from bars. Everything downstream — planner, sizing, sl_rules, staging,
    metrics — is byte-identical either way, which is the point of emitting the
    existing `ParsedSignal` shape rather than a parallel one."""
    id: int
    at: dt.datetime
    parsed: ParsedSignal
    source_id: Optional[int] = None
    source_name: Optional[str] = None
    account_ids: tuple = ()


@dataclass
class VariantResult:
    variant: str
    trades: List[sim.SimTrade] = field(default_factory=list)
    not_taken: List[dict] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    coverage: dict = field(default_factory=dict)

    @property
    def n_signals_blocked_by_caps(self) -> int:
        return self.counts["risk_limit_block"] + self.counts["breaker_block"]


class PortfolioSim:
    """Replays one variant over one signal set against one bar series."""

    def __init__(self, variant: Variant, series: B.BarSeries,
                 ctx: Optional[ContextBuilder] = None):
        self.variant = variant
        self.series = series
        self.ctx = ctx or ContextBuilder(series)

    # --- risk state -----------------------------------------------------------
    @staticmethod
    def _day(ts: dt.datetime) -> dt.date:
        return ts.date()

    def _open_risk(self, live: Iterable[sim.SimTrade], account_id: int,
                   symbol: Optional[str] = None) -> Decimal:
        total = Decimal("0")
        for t in live:
            if t.account_id != account_id:
                continue
            if symbol is not None and t.symbol != symbol:
                continue
            total += t.planned_risk
        return total

    # --- the run --------------------------------------------------------------
    def run(self, signals: Iterable[SignalRow]) -> VariantResult:
        v = self.variant
        res = VariantResult(variant=v.name, coverage=self.series.coverage())
        rows = sorted(signals, key=lambda s: (s.at, s.id))
        if not rows or not len(self.series):
            res.counts["no_signals" if not rows else "no_candles"] += len(rows)
            for s in rows:
                res.not_taken.append({"signal_id": s.id, "account_id": None,
                                      "reason": "no_candles"})
            return res

        # Bucket arrivals by bar index so the main loop is a single forward pass.
        arrivals: Dict[int, List[tuple]] = defaultdict(list)
        for s in rows:
            mc = self.ctx.build(s.at, filter_rules=None)
            if mc is None:
                res.counts["no_candle_coverage"] += 1
                res.not_taken.append({"signal_id": s.id, "account_id": None,
                                      "reason": "no_candle_coverage"})
                continue
            arrivals[mc.bar_index].append((s, mc.bar_index))

        if not arrivals:
            return res

        live: List[sim.SimTrade] = []
        deadlines: Dict[int, int] = {}          # id(trade) -> last bar index
        day_realized: Dict[tuple, Decimal] = defaultdict(Decimal)
        cooldown: Dict[int, Optional[dt.datetime]] = {}

        start = min(arrivals)
        end = len(self.series) - 1
        for i in range(start, end + 1):
            bar = self.series[i]
            for s, _ in arrivals.get(i, ()):     # arrivals first: a MARKET leg
                self._admit(s, bar, live, deadlines, day_realized, cooldown, res, i)
            if not live:
                if i > max(arrivals):
                    break                        # nothing left to arrive or manage
                continue
            still: List[sim.SimTrade] = []
            for t in live:
                if i > deadlines.get(id(t), end):
                    sim.finish(t, bar, variant=v)
                else:
                    sim.step(t, bar, variant=v)
                if t.is_done:
                    self._retire(t, day_realized, res)
                else:
                    still.append(t)
            live = still

        # Anything still open when the bars ran out is horizon-capped, not a win.
        last = self.series[end] if len(self.series) else None
        for t in live:
            sim.finish(t, last, variant=v)
            self._retire(t, day_realized, res)
        return res

    # --- admission ------------------------------------------------------------
    def _admit(self, s: SignalRow, bar: B.Bar, live: List[sim.SimTrade],
               deadlines: Dict[int, int], day_realized, cooldown,
               res: VariantResult, bar_index: int) -> None:
        v = self.variant
        accounts = s.account_ids or tuple(a.id for a in v.accounts)
        if not accounts:
            res.counts["no_account_mapping"] += 1
            res.not_taken.append({"signal_id": s.id, "account_id": None,
                                  "reason": "no_account_mapping"})
            return
        for account_id in accounts:
            cfg = v.resolve(account_id, s.source_id)
            mc = self.ctx.build(
                s.at, filter_rules=cfg.filter_rules,
                need_staged_atr=str(cfg.entry_policy.get("entry_style") or "") == "staged")
            if mc is None:
                self._reject(res, s, account_id, "no_candle_coverage")
                continue

            # --- filtration pillar. Fail-open on an evaluator error, exactly as
            # the executor does (#164): a filtration rule must never be able to
            # delete a signal by being wrong about its own inputs.
            if cfg.filter_rules:
                try:
                    d = ST.evaluate_filter_rules(cfg.filter_rules, filter_ctx(mc))
                except Exception:
                    d = None
                if d is not None and d.skip:
                    self._reject(res, s, account_id, "filtration_skip",
                                 detail={"rules": list(d.reasons)})
                    continue
                if d is not None and d.factor != 1.0:
                    cfg = _scaled(cfg, d.factor)

            trade, why = sim.plan_trade(
                signal=s.parsed, signal_id=s.id, source_id=s.source_id,
                account_id=account_id, signal_at=s.at, cfg=cfg, variant=v, mc=mc)
            if trade is None:
                self._reject(res, s, account_id, why or "not_planned")
                continue

            # --- risk limits, on the SAME basis the executor uses: the day's
            # realized for trades CREATED today, and the summed planned_risk of
            # everything still live. Modelling these is not optional — §5.
            key = (account_id, self._day(s.at))
            reason = risk_limit_reason(
                planned_risk=trade.planned_risk,
                day_realized=day_realized[key],
                open_risk_symbol=self._open_risk(live, account_id, trade.symbol),
                open_risk_account=self._open_risk(live, account_id),
                cfg=v.risk_limits)
            if reason:
                self._reject(res, s, account_id, "risk_limit_block",
                             detail={"detail": reason,
                                     "planned_risk": str(trade.planned_risk)})
                continue
            bd = soft_breaker_decision(day_realized=day_realized[key],
                                       cfg=v.risk_limits, now=s.at,
                                       cooldown_until=cooldown.get(account_id))
            cooldown[account_id] = bd["cooldown_until"]
            if bd["block"]:
                self._reject(res, s, account_id, "breaker_block",
                             detail={"detail": bd["reason"]})
                continue

            trade.strategy_label = _label(v, account_id, s.source_id)
            live.append(trade)
            deadlines[id(trade)] = bar_index + max(1, int(v.horizon_bars))
            res.counts["taken"] += 1

    def _reject(self, res: VariantResult, s: SignalRow, account_id, reason: str,
                detail: Optional[dict] = None) -> None:
        """`reason` is the BUCKET and is authoritative — extra detail can never
        overwrite it. It did once, which silently turned every risk-limit block
        into its own free-text bucket and made the blocked count unreadable."""
        res.counts[reason] += 1
        row = {"signal_id": s.id, "account_id": account_id,
               "source_id": s.source_id}
        if detail:
            row.update(detail)
        row["reason"] = reason
        res.not_taken.append(row)

    def _retire(self, t: sim.SimTrade, day_realized, res: VariantResult) -> None:
        day_realized[(t.account_id, self._day(t.signal_at))] += t.realized_pl
        res.trades.append(t)
        if not t.ever_filled:
            res.counts["never_filled"] += 1
        if t.horizon_capped:
            res.counts["horizon_capped"] += 1
        res.counts["same_bar_ambiguous"] += t.same_bar_ambiguous


def _scaled(cfg, factor: float):
    """Apply a filtration `scale` action the way the executor does — multiply the
    risk config's value (and each per-TP percent), never the lot afterwards, so
    the cap and min-lot checks see the de-sized plan."""
    from dataclasses import replace
    from beacon_core.risk.sizing import RiskConfig
    f = Decimal(str(factor))
    r = cfg.risk
    scaled = RiskConfig(
        basis=r.basis, value=r.value * f, allocation=r.allocation,
        per_tp_percent=({k: v * f for k, v in r.per_tp_percent.items()}
                        if r.per_tp_percent else None),
        per_tp_split_across_entries=r.per_tp_split_across_entries)
    return replace(cfg, risk=scaled)


def _label(v: Variant, account_id, source_id) -> str:
    """The arm label `geometry_ab_rollup` groups by. Falls back to the variant
    name so a run always has an arm identity even when no strategy row names one."""
    chain = ST.resolve_chain(v.strategies, account_id, source_id)
    for s in chain:
        if getattr(s, "label", None):
            return s.label
    return v.name
