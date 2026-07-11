"""Working-order TTL clamp (#40): per-channel entry_ttl_minutes must fall back
to a safe default and never exceed the cap, so an order can't rest as GTC."""
from beacon_core.config import (effective_entry_ttl_min, DEFAULT_ENTRY_TTL_MIN,
                                MIN_ENTRY_TTL_MIN, MAX_ENTRY_TTL_MIN)


def test_default_when_absent_or_invalid():
    assert effective_entry_ttl_min(None) == DEFAULT_ENTRY_TTL_MIN
    assert effective_entry_ttl_min({}) == DEFAULT_ENTRY_TTL_MIN
    assert effective_entry_ttl_min({"entry_ttl_minutes": "oops"}) == DEFAULT_ENTRY_TTL_MIN
    assert effective_entry_ttl_min({"entry_ttl_minutes": None}) == DEFAULT_ENTRY_TTL_MIN


def test_per_channel_value_respected():
    assert effective_entry_ttl_min({"entry_ttl_minutes": 60}) == 60
    assert effective_entry_ttl_min({"entry_ttl_minutes": 480}) == 480     # 8h slow-fill
    assert effective_entry_ttl_min({"entry_ttl_minutes": "120"}) == 120   # numeric string


def test_clamped_to_bounds():
    # can't be set to GTC (0 / negative / huge) by accident
    assert effective_entry_ttl_min({"entry_ttl_minutes": 0}) == MIN_ENTRY_TTL_MIN
    assert effective_entry_ttl_min({"entry_ttl_minutes": -5}) == MIN_ENTRY_TTL_MIN
    assert effective_entry_ttl_min({"entry_ttl_minutes": 100000}) == MAX_ENTRY_TTL_MIN
    assert MIN_ENTRY_TTL_MIN >= 1 and MAX_ENTRY_TTL_MIN <= 1440


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
