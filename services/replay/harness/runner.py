"""Sweep orchestration: N variants over one signal set, deterministically (§4).

SCALE-OUT BY DESIGN, and the design is boring on purpose:

  * **Job per unit, stateless workers.** The unit is one `(variant, signal set)`
    portfolio replay (see `portfolio.py` for why the unit is not per-signal).
    Jobs share nothing — no cross-job state, no ordering dependency — so the
    same code runs one-process, N-process, or later N-container with no rewrite.
  * **The bars are loaded once.** A worker receives the series through a pool
    initializer, not per job: shipping 400k bars per variant would make the
    transport cost the run.
  * **Deterministic.** No RNG, no wall clock, no `dict` iteration order that
    isn't sorted first. Results are assembled by variant name, not by completion
    order, so a two-worker run and a one-worker run produce byte-identical
    output. That is what makes "re-running a run_id reproduces exactly" true
    rather than usually-true.

PURE — stdlib + beacon_core. The DB is `store.py`'s job, and `main.py` wires the
two together, so the whole sweep is unit-testable on a bare box.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from . import metrics
from . import bars as B
from .context import ContextBuilder
from .portfolio import PortfolioSim, SignalRow, VariantResult
from .variants import Variant, build_variant, canonical_digest

CODE_VERSION = "1.0.0"


@dataclass
class RunSpec:
    """A run as data: what to replay, over what, under which variants."""
    label: str = ""
    symbol: str = "XAUUSD"
    timeframe: str = "1m"
    frm: Optional[dt.datetime] = None
    to: Optional[dt.datetime] = None
    holdout_from: Optional[dt.datetime] = None
    signal_source: str = "historical"
    generator_config: dict = field(default_factory=dict)
    variants: List[dict] = field(default_factory=list)
    workers: int = 0
    source_ids: Optional[List[int]] = None
    account_ids: Optional[List[int]] = None

    def digest(self) -> str:
        return canonical_digest({
            "label": self.label, "symbol": self.symbol, "timeframe": self.timeframe,
            "frm": _iso(self.frm), "to": _iso(self.to),
            "holdout_from": _iso(self.holdout_from),
            "signal_source": self.signal_source,
            "generator_config": self.generator_config,
            "variants": self.variants,
            "source_ids": self.source_ids, "account_ids": self.account_ids,
            "code_version": CODE_VERSION,
        })


def _iso(d):
    return d.isoformat() if d is not None else None


GIT_SHA_ENV = "BEACON_GIT_SHA"


def git_sha(cwd: Optional[str] = None) -> Optional[str]:
    """The commit the harness ran at. Recorded on the run so a result is always
    attributable to a version of the logic that produced it — a replay whose code
    cannot be identified is not reproducible, only repeatable.

    THE ENV VAR IS CHECKED FIRST, AND IN A CONTAINER IT IS THE ONLY THING THAT
    WORKS. The image COPYs the source in; there is no `.git` and no `git` binary,
    so shelling out returns None and always has — every run stored before this
    was written carries `git_sha = NULL`, which quietly made #169's central
    reproducibility claim unenforceable and blocked a sweep from inheriting a
    gate verdict (there was no code identity to match on). The Dockerfile bakes
    it from a build arg:

        GIT_SHA=$(git rev-parse HEAD) docker compose build replay

    The subprocess fallback is for running the harness straight from a checkout,
    where it does work. Both paths can still return None — that is reported
    loudly by the caller rather than swallowed, because an unattributable run is
    a real limitation, not a detail."""
    env = (os.getenv(GIT_SHA_ENV) or "").strip()
    if env:
        return env[:40]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd or os.getcwd(),
                             capture_output=True, text=True, timeout=10)
        sha = (out.stdout or "").strip()
        return sha[:40] or None
    except Exception:
        return None


# --- one job ------------------------------------------------------------------
def run_variant(variant: Variant, series: B.BarSeries, signals: Sequence[SignalRow],
                ctx: Optional[ContextBuilder] = None) -> VariantResult:
    return PortfolioSim(variant, series, ctx=ctx).run(signals)


# --- the sweep ----------------------------------------------------------------
_POOL_STATE: dict = {}


def _pool_init(series: B.BarSeries, signals: List[SignalRow]) -> None:
    """Runs ONCE per worker process. The bars and the resampled TA frames are
    built here and reused across every variant that worker handles."""
    _POOL_STATE["series"] = series
    _POOL_STATE["signals"] = signals
    _POOL_STATE["ctx"] = ContextBuilder(series)


def _pool_job(variant_dict: dict) -> tuple:
    v = build_variant(variant_dict)
    res = run_variant(v, _POOL_STATE["series"], _POOL_STATE["signals"],
                      ctx=_POOL_STATE["ctx"])
    return v.name, res


def sweep(spec: RunSpec, series: B.BarSeries, signals: Sequence[SignalRow],
          *, sources_by_id: Optional[dict] = None,
          generator_stats: Optional[dict] = None) -> dict:
    """Run every variant and build the reports.

    `n_variants_searched` is threaded into every report: the best of N backtests
    is upward-biased by construction, and a winner shown without its N is
    misleading (§8.2). It is not something the reader has to remember to ask for.
    """
    variants = list(spec.variants or [])
    n = len(variants)
    if not n:
        return {"run": _run_header(spec, series, generator_stats),
                "variants": {}, "results": {}}

    signals = list(signals)
    pairs: List[tuple] = []
    if spec.workers and spec.workers > 1 and n > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(spec.workers, n),
                                 initializer=_pool_init,
                                 initargs=(series, signals)) as pool:
            pairs = list(pool.map(_pool_job, variants))
    else:
        ctx = ContextBuilder(series)                # resample once for all variants
        for vd in variants:
            v = build_variant(vd)
            pairs.append((v.name, run_variant(v, series, signals, ctx=ctx)))

    # Assembled by NAME, sorted — never by completion order, or a parallel run
    # would differ from a sequential one in the one way that matters.
    results = {name: res for name, res in pairs}
    reports = {}
    for vd in variants:
        v = build_variant(vd)
        res = results.get(v.name)
        if res is None:
            continue
        reports[v.name] = metrics.variant_report(
            res, variant=v, series=series, holdout_from=spec.holdout_from,
            n_variants_searched=n, sources_by_id=sources_by_id)
    return {"run": _run_header(spec, series, generator_stats),
            "variants": {k: reports[k] for k in sorted(reports)},
            "results": results}


def _run_header(spec: RunSpec, series: B.BarSeries,
                generator_stats: Optional[dict] = None) -> dict:
    head = {
        "label": spec.label, "symbol": spec.symbol, "timeframe": spec.timeframe,
        "from": _iso(spec.frm), "to": _iso(spec.to),
        "holdout_from": _iso(spec.holdout_from),
        "signal_source": spec.signal_source,
        "n_variants": len(spec.variants or []),
        "config_digest": spec.digest(), "code_version": CODE_VERSION,
        "coverage": series.coverage(),
        "promotion": ("Replay results are HYPOTHESIS-GENERATING, not "
                      "promotion-grade. The live frozen-week A/B/C on a frozen "
                      "config remains the only thing that promotes a config "
                      "(CLAUDE.md §2; weekly manual Output 4)."),
    }
    if generator_stats:
        # A generated run's signal set is an OUTPUT of the config, not an input
        # read from the ledger. What the generator suppressed and dropped
        # therefore belongs on the run header beside the coverage — a reader who
        # sees only the emitted count cannot tell a selective strategy from one
        # the cooldown throttled (#184).
        head["generator"] = dict(generator_stats)
    return head


def compare_variants(reports: Dict[str, dict], metric: str = "expectancy_R") -> List[dict]:
    """Rank variants by a headline metric, carrying the guardrails along.

    The ranking deliberately reports `verdict_withheld` next to the number: a
    variant that wins on 11 trades has not won, and a table that lets the eye
    read the ordering without the N is the failure mode this whole section
    exists to prevent."""
    rows = []
    for name in sorted(reports):
        rep = reports[name]
        head = rep.get("headline") or {}
        arms = head.get("by_arm") or []
        best = None
        for arm in arms:
            val = arm.get(metric)
            if val is not None and (best is None or val > best[0]):
                best = (val, arm)
        rows.append({
            "variant": name,
            "basis": rep.get("headline_basis"),
            metric: None if best is None else best[0],
            "n_closed": rep.get("n_closed"),
            "verdict_withheld": rep.get("verdict_withheld"),
            "n_variants_searched": (rep.get("guardrails") or {}).get("n_variants_searched"),
            "best_of_n_inflation_sigma": (rep.get("guardrails") or {}).get(
                "best_of_n_inflation_sigma"),
        })
    rows.sort(key=lambda r: (r[metric] is None, -(r[metric] or 0), r["variant"]))
    return rows
