"""Config-as-data: the scope cascade, the risk resolution, and the digest.

The cascade is not reimplemented here — `Variant.resolve` calls the shipped
`execution.strategy` functions on duck-typed rows. These tests exist to prove
that a plain dict really does resolve the way an `execution_strategies` row
does, because if it does not, every per-channel variant is silently wrong.
"""
from __future__ import annotations

from decimal import Decimal

from harness.variants import build_variant, canonical_digest
from conftest import variant_dict


def _per_channel():
    return build_variant(variant_dict(name="mixed", strategies=[
        {"account_id": None, "source_id": None, "label": "base",
         "entry_policy": {"entry_style": "limit", "ttl_minutes": 45},
         "exit_policy": {"sl_rules": [
             {"trigger": {"type": "tp_hit", "index": 1},
              "action": {"type": "move_sl_to", "target": "entry"}}]}},
        {"account_id": 1, "source_id": 7, "label": "src7",
         "exit_policy": {"sl_rules": [
             {"trigger": {"type": "tp_hit", "index": 2},
              "action": {"type": "move_sl_to", "target": "entry"}}]}},
    ]))


def test_a_more_specific_row_overrides_only_the_pillar_it_sets():
    """"BE@TP2 for TFXC but BE@TP1 for Yulia" must be ONE variant, and the (1,7)
    row must inherit the entry policy it does not restate."""
    v = _per_channel()
    src7 = v.resolve(1, 7)
    other = v.resolve(1, 9)
    assert src7.sl_rules[0]["trigger"]["index"] == 2
    assert other.sl_rules[0]["trigger"]["index"] == 1
    # inherited from the (Any, Any) base row, not from a code default
    assert src7.entry_policy["ttl_minutes"] == 45
    assert src7.ttl_minutes == 45


def test_an_unset_exit_pillar_cascades_to_the_built_in_default():
    v = build_variant(variant_dict(strategies=[
        {"account_id": None, "source_id": None, "entry_policy": {}}]))
    cfg = v.resolve(1, 7)
    assert cfg.sl_rules_origin == "default"
    assert cfg.sl_rules, "an unset ladder must fall back, never be empty"


def test_the_entry_ttl_is_clamped_by_the_shipped_helper():
    """A variant must not be able to express a TTL the executor would refuse."""
    v = build_variant(variant_dict(entry_policy={"ttl_minutes": 999999}))
    from beacon_core.config import MAX_ENTRY_TTL_MIN
    assert v.resolve(1, 7).ttl_minutes == MAX_ENTRY_TTL_MIN


def test_a_per_account_source_risk_override_wins_over_the_account_default():
    v = build_variant(variant_dict(risk={
        "default": {"basis": "fixed_cash", "value": 100},
        "by_account": {"1": {"basis": "fixed_cash", "value": 200}},
        "by_account_source": {"1:7": {"basis": "fixed_cash", "value": 50}},
    }))
    assert v.resolve(1, 7).risk.value == Decimal("50")
    assert v.resolve(1, 9).risk.value == Decimal("200")
    assert v.resolve(2, 9).risk.value == Decimal("100")


def test_a_missing_risk_limits_block_fails_SAFE_not_open():
    """An unconfigured install trades with conservative defaults live; a variant
    that forgets the block must not get an uncapped backtest."""
    d = variant_dict()
    d.pop("risk_limits")
    v = build_variant(d)
    assert v.risk_limits.get("enabled") is True
    assert v.risk_limits.get("daily_loss_limit")


def test_resolution_is_cached_but_not_shared_between_scopes():
    v = _per_channel()
    assert v.resolve(1, 7) is v.resolve(1, 7)
    assert v.resolve(1, 7) is not v.resolve(1, 9)


def test_the_digest_is_content_addressed_and_order_independent():
    a = canonical_digest({"a": 1, "b": [1, 2]})
    b = canonical_digest({"b": [1, 2], "a": 1})
    assert a == b
    assert a != canonical_digest({"a": 1, "b": [2, 1]})


def test_two_variants_that_differ_have_different_digests():
    x = build_variant(variant_dict(name="x", sl_rules=[
        {"trigger": {"type": "tp_hit", "index": 1},
         "action": {"type": "move_sl_to", "target": "entry"}}]))
    y = build_variant(variant_dict(name="x", sl_rules=[
        {"trigger": {"type": "tp_hit", "index": 2},
         "action": {"type": "move_sl_to", "target": "entry"}}]))
    assert x.digest() != y.digest()


def test_unknown_keys_survive_into_the_digest_but_are_not_interpreted():
    """A typo must not silently become a default — it changes the digest, so a
    re-run that behaves differently is traceable to the config."""
    base = build_variant(variant_dict())
    typo = build_variant(variant_dict(horizonbars=99))
    assert base.digest() != typo.digest()
    assert typo.horizon_bars == base.horizon_bars
