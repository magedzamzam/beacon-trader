"""Test fixtures for the replay harness.

Everything here is synthetic and stdlib-only: the whole simulator is pure, so
the suite runs on a bare box with no DB, no Redis and no broker — the same
convention `packages/core/tests` follows.
"""
from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# `services/replay` on the path so `import harness` resolves the same way it does
# in the container (WORKDIR /app). The package is `harness`, not `app`, because
# `services/api` already owns an `app` package and whichever conftest ran first
# would shadow the other's.
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from harness import bars as B                                       # noqa: E402
from harness.portfolio import SignalRow                             # noqa: E402
from harness.variants import build_variant                          # noqa: E402
from beacon_core.parsing.models import ParsedSignal             # noqa: E402

T0 = dt.datetime(2026, 7, 6, 12, 0, tzinfo=dt.timezone.utc)
SPREAD = 0.2


def bar(ts, o, h, l, c, *, spread: float = SPREAD, volume=None) -> B.Bar:
    """One bar from MID OHLC plus a symmetric spread. Bid = mid - s/2,
    ask = mid + s/2 — a clean, crossing-free quote so a test that cares about
    the spread can say so explicitly rather than inheriting noise."""
    h2 = spread / 2
    return B.Bar(ts=ts,
                 open_bid=o - h2, open_ask=o + h2,
                 high_bid=h - h2, high_ask=h + h2,
                 low_bid=l - h2, low_ask=l + h2,
                 close_bid=c - h2, close_ask=c + h2, volume=volume)


def path_bars(mids, *, start=T0, spread: float = SPREAD, step_minutes: int = 1):
    """A 1m series from a list of mid prices. Each entry is either a scalar
    (a flat bar at that price) or an (o, h, l, c) tuple."""
    out = []
    for i, p in enumerate(mids):
        ts = start + dt.timedelta(minutes=i * step_minutes)
        if isinstance(p, (tuple, list)):
            o, h, l, c = p
        else:
            o = h = l = c = float(p)
        out.append(bar(ts, float(o), float(h), float(l), float(c), spread=spread))
    return out


def series(mids, **kw) -> B.BarSeries:
    return B.BarSeries(path_bars(mids, **kw))


def signal(*, direction="BUY", entry=4000.0, entry_to=None, sl=3990.0,
           tps=(4010.0, 4020.0, 4030.0), hint=None) -> ParsedSignal:
    return ParsedSignal(
        symbol="XAUUSD", direction=direction,
        entry_from=Decimal(str(entry)),
        entry_to=Decimal(str(entry if entry_to is None else entry_to)),
        sl=Decimal(str(sl)), tps=[Decimal(str(t)) for t in tps],
        order_type_hint=hint, raw_text="test")


def signal_row(parsed, *, at=None, sid=1, source_id=7, accounts=(1,)) -> SignalRow:
    return SignalRow(id=sid, at=at or (T0 + dt.timedelta(minutes=1)),
                     parsed=parsed, source_id=source_id, source_name="src",
                     account_ids=tuple(accounts))


def sl_rules_be_at(index: int) -> list:
    return [{"trigger": {"type": "tp_hit", "index": index},
             "action": {"type": "move_sl_to", "target": "entry"}}]


# An EMPTY sl_rules list does not mean "no ratchet": `exit_sl_rules` treats a
# falsy pillar as UNSET and cascades to DEFAULT_SL_RULES (the BE@TP1 ladder), so
# a control arm has to be expressed as a rule that can never fire.
NO_RATCHET = [{"trigger": {"type": "tp_hit", "index": 99},
               "action": {"type": "move_sl_to", "target": "entry"}}]


def variant_dict(*, name="v", sl_rules=None, entry_policy=None, risk=None,
                 risk_limits=None, accounts=None, strategies=None, **kw) -> dict:
    """A minimal but COMPLETE variant. `value_per_point = 1` and `lot_step`
    0.001 keep the money arithmetic exact so a test can assert `-risk_cash` at
    the stop rather than a rounded approximation of it."""
    d = {
        "name": name,
        "accounts": accounts or [{"id": 1, "name": "A", "equity": 10000,
                                  "currency": "USD", "fx_factor": 1}],
        "risk": risk or {"default": {"basis": "fixed_cash", "value": 100,
                                     "allocation": "even"}},
        "risk_limits": risk_limits if risk_limits is not None else {
            "enabled": True, "daily_loss_limit": 0, "max_open_risk_per_symbol": 0,
            "max_open_risk_per_account": 0, "max_signal_risk_pct": 0},
        "instrument": {"value_per_point": 1, "min_lot": 0.001, "lot_step": 0.001},
        "costs": {"slippage_points": 0.0},
        "horizon_bars": 500,
    }
    d["strategies"] = strategies if strategies is not None else [{
        "account_id": None, "source_id": None, "label": name,
        "entry_policy": entry_policy or {"entry_style": "limit", "ttl_minutes": 60},
        "exit_policy": {"cancel_pending_on_stop": True,
                        "sl_rules": sl_rules if sl_rules is not None else []},
    }]
    d.update(kw)
    return d


@pytest.fixture
def simple_variant():
    return build_variant(variant_dict())
