"""Instrument classification helpers — kept broker-agnostic and swappable.

`asset_class` decides which data pipelines apply (e.g. crypto microstructure).
`binance_symbol` maps an internal crypto symbol to its Binance USDT-perp ticker
for the free public microstructure feeds.
"""
from __future__ import annotations

_CRYPTO_BASES = ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LTC", "BCH",
                 "AVAX", "LINK", "DOT", "MATIC", "TRX")

# Explicit internal -> Binance USDT-perp overrides; everything else falls back
# to "<base>USDT" when the internal symbol ends in USD.
_BINANCE_OVERRIDES = {
    "BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT", "SOLUSD": "SOLUSDT",
    "XRPUSD": "XRPUSDT", "BNBUSD": "BNBUSDT", "ADAUSD": "ADAUSDT",
    "DOGEUSD": "DOGEUSDT", "LTCUSD": "LTCUSDT",
}


def asset_class(symbol: str) -> str:
    """crypto | gold | metal | fx | other, from the internal symbol string."""
    s = (symbol or "").upper()
    if any(b in s for b in _CRYPTO_BASES):
        return "crypto"
    if s.startswith("XAU") or "GOLD" in s:
        return "gold"
    if s.startswith("XAG") or "SILVER" in s:
        return "metal"
    if len(s) == 6 and s.isalpha():
        return "fx"
    return "other"


def is_crypto(symbol: str) -> bool:
    return asset_class(symbol) == "crypto"


def binance_symbol(symbol: str) -> str | None:
    """Internal crypto symbol -> Binance USDT-perp ticker, or None if not crypto."""
    s = (symbol or "").upper()
    if s in _BINANCE_OVERRIDES:
        return _BINANCE_OVERRIDES[s]
    if is_crypto(s) and s.endswith("USD"):
        return s[:-3] + "USDT"
    return None
