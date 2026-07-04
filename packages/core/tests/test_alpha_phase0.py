"""Unit tests for Alpha Layer Phase 0 pure helpers (no DB / network)."""
import datetime as dt
from decimal import Decimal

from beacon_core.marketsessions import session_for
from beacon_core.instruments import asset_class, is_crypto, binance_symbol
from beacon_core.alpha.crypto_micro import liquidation_proxy


def _utc(h):
    return dt.datetime(2026, 7, 3, h, 30, tzinfo=dt.timezone.utc)


def test_session_boundaries():
    assert session_for(_utc(0)) == "ASIA"
    assert session_for(_utc(6)) == "ASIA"
    assert session_for(_utc(7)) == "LONDON"
    assert session_for(_utc(11)) == "LONDON"
    assert session_for(_utc(12)) == "OVERLAP"
    assert session_for(_utc(15)) == "OVERLAP"
    assert session_for(_utc(16)) == "NY"
    assert session_for(_utc(20)) == "NY"
    assert session_for(_utc(21)) == "LATE"
    assert session_for(_utc(23)) == "LATE"


def test_session_naive_is_utc():
    assert session_for(dt.datetime(2026, 7, 3, 8, 0)) == "LONDON"


def test_session_tz_conversion():
    # 09:00 in UTC+3 == 06:00 UTC -> ASIA
    tz = dt.timezone(dt.timedelta(hours=3))
    assert session_for(dt.datetime(2026, 7, 3, 9, 0, tzinfo=tz)) == "ASIA"


def test_asset_class():
    assert asset_class("XAUUSD") == "gold"
    assert asset_class("BTCUSD") == "crypto"
    assert asset_class("ETHUSD") == "crypto"
    assert asset_class("EURUSD") == "fx"
    assert asset_class("XAGUSD") == "metal"
    assert is_crypto("BTCUSD") and not is_crypto("EURUSD")


def test_binance_symbol():
    assert binance_symbol("BTCUSD") == "BTCUSDT"
    assert binance_symbol("SOLUSD") == "SOLUSDT"
    assert binance_symbol("EURUSD") is None
    assert binance_symbol("XAUUSD") is None


def _flat(n, price="100"):
    p = Decimal(price)
    return [{"h": p, "l": p, "c": p} for _ in range(n)]


def test_liquidation_proxy_quiet_market_false():
    assert liquidation_proxy(_flat(20)) is False


def test_liquidation_proxy_spike_with_retrace_true():
    # 15 quiet 1-wide bars, then a 12-wide down spike, then a retrace back up.
    bars = [{"h": Decimal("101"), "l": Decimal("100"), "c": Decimal("100.5")} for _ in range(15)]
    bars.append({"h": Decimal("100"), "l": Decimal("88"), "c": Decimal("88.5")})   # forced move down
    bars.append({"h": Decimal("95"), "l": Decimal("88"), "c": Decimal("95")})       # retrace up >= 50%
    bars.append({"h": Decimal("96"), "l": Decimal("94"), "c": Decimal("95")})
    bars.append({"h": Decimal("96"), "l": Decimal("94"), "c": Decimal("95")})
    assert liquidation_proxy(bars, k=Decimal("3"), m=3) is True


def test_liquidation_proxy_spike_no_retrace_false():
    bars = [{"h": Decimal("101"), "l": Decimal("100"), "c": Decimal("100.5")} for _ in range(15)]
    bars.append({"h": Decimal("100"), "l": Decimal("88"), "c": Decimal("88.5")})   # forced move down
    bars.append({"h": Decimal("89"), "l": Decimal("87"), "c": Decimal("88")})       # keeps falling, no retrace
    bars.append({"h": Decimal("88"), "l": Decimal("86"), "c": Decimal("87")})
    bars.append({"h": Decimal("87"), "l": Decimal("85"), "c": Decimal("86")})
    assert liquidation_proxy(bars, k=Decimal("3"), m=3) is False
