"""Trading Hours — session windows, news blackout, and holiday/weekend status.

Sessions and holidays are computed live (no external data; DST handled via
zoneinfo). News comes from a swappable free economic-calendar feed and is
persisted. This module exposes the current status so the operator can *see* it
and so trade gating can later be built on top of it — nothing here blocks
trading yet.
"""
