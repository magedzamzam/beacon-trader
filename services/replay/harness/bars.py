"""Moved to `beacon_core.market.bars` (#224).

A live producer needs the same resampling and the same closed-bar boundary the
backtest uses, and the one-way rule means shared code lives in `beacon_core`.
Re-exported here so every existing `from . import bars as B` keeps working and
there is still exactly one implementation.
"""
from beacon_core.market.bars import *          # noqa: F401,F403
from beacon_core.market.bars import (           # noqa: F401  (explicit: `*` skips _names)
    Bar, BarSeries, OhlcBar, resample, timeframe_minutes)
