"""Parser: a pip count is not a price (#162).

Gold's band starts at 1500, so a trailing "(1500 pips)" was collected as a
candidate price. For a BUY the risk numbers sort ascending, so the pip count
took the SL slot and the real SL was displaced into the TPs — the signal then
failed validation ("BUY has a take-profit at/below entry") and was lost. Same
message with "(1000 pips)" parsed fine, which is why it looked intermittent.
"""
from decimal import Decimal

from beacon_core.parsing import parse
from beacon_core.execution.planner import validate_signal


def _msg(direction, entry, tps, sl, pips):
    return ("SIGNAL ALERT\n\n%s XAUUSD %s\n\n"
            "\U0001f911TP1: %s\n\U0001f911TP2: %s\n\U0001f911TP3: %s\n"
            "\U0001f534SL: %s (%s pips)" % (direction, entry, tps[0], tps[1], tps[2], sl, pips))


# The exact message that was rejected in production (signal #771, 2026-07-27).
BUY_1500 = _msg("BUY", "4083.4", ["4085.7", "4087.9", "4098.4"], "4068.4", 1500)
BUY_1000 = _msg("BUY", "4097.4", ["4098.9", "4100.4", "4107.4"], "4087.4", 1000)
SELL_1500 = _msg("SELL", "4085.3", ["4083.2", "4081.1", "4071.3"], "4099.3", 1500)


def test_buy_pip_count_no_longer_becomes_the_stop_loss():
    p = parse(BUY_1500)
    assert p is not None
    assert p.entry_from == Decimal("4083.4") and p.entry_to == Decimal("4083.4")
    assert p.sl == Decimal("4068.4")
    assert p.tps == [Decimal("4085.7"), Decimal("4087.9"), Decimal("4098.4")]
    ok, reason = validate_signal(p)
    assert ok, reason


def test_sell_pip_count_does_not_leak_into_the_tp_ladder():
    # Survived validation before the fix (a TP below entry is legal for a SELL)
    # and was only dropped downstream by the planner's distance bound (#13).
    p = parse(SELL_1500)
    assert p is not None
    assert p.sl == Decimal("4099.3")
    assert p.tps == [Decimal("4083.2"), Decimal("4081.1"), Decimal("4071.3")]
    assert Decimal("1500") not in p.tps
    ok, reason = validate_signal(p)
    assert ok, reason


def test_below_band_pip_count_still_parses_unchanged():
    # Regression guard: 1000 was already filtered by the band floor.
    p = parse(BUY_1000)
    assert p is not None
    assert p.sl == Decimal("4087.4")
    assert p.tps == [Decimal("4098.9"), Decimal("4100.4"), Decimal("4107.4")]
    assert validate_signal(p)[0]


def test_raw_text_is_preserved():
    p = parse(BUY_1500)
    assert p.raw_text == BUY_1500 and "1500 pips" in p.raw_text


def test_pip_stripping_does_not_promote_recaps_to_signals():
    # Commentary that mentions pips must still not parse as a tradeable signal.
    for text in (
        "Quick update, everyone.\n1️⃣Sell 4030-4132 TP+110pips✅\n"
        "2️⃣Sell 4036-4032 TP + 50pips✅",
        "\U0001f3afTP1 HIT 4026\nBuy entry 4019: + 70pips✅\n"
        "Buy entry 4020: + 60pips✅",
        "\U0001f3afStoploss HIT 4051\nSell entry 4045: -60pips\n"
        "Sell entry 4046: -50pips",
    ):
        assert parse(text) is None


def test_inline_and_unparenthesised_pip_counts_are_stripped():
    # Same poison, different punctuation: no parens, no space.
    p = parse("BUY XAUUSD 4083.4\nTP1: 4085.7\nTP2: 4087.9\nTP3: 4098.4\nSL: 4068.4 1500pips")
    assert p is not None and p.sl == Decimal("4068.4")
    assert Decimal("1500") not in p.tps
