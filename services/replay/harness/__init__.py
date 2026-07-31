"""Offline replay + backtest harness (#169).

Research half of the #60 ADR. The one-way rule holds: this package may import
`beacon_core`; `beacon_core` and the trading services must never import this.
"""
