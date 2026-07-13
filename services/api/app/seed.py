"""Idempotent seed: a demo Capital.com broker (credentials entered later in the
portal — never from .env), one account with a default risk config, an XAUUSD
symbol map, and two sample sources (a telegram channel + a manual desk) wired
with SL rules, and per-TP risk. Safe to run repeatedly.

    docker compose run --rm api python -m app.seed

The seeded broker is created disabled and WITHOUT credentials: add its API
key / username / password in the portal (Configuration -> Brokers) so they are
stored encrypted in the DB.
"""
import asyncio
from decimal import Decimal

from sqlalchemy import select

from beacon_core.db.base import Session, init_models
from beacon_core.db.models import Account, Broker, Source, SymbolMap
from beacon_core.execution.guard import DEFAULT_RISK_LIMITS
from beacon_core.strategy.rules import DEFAULT_SL_RULES
from beacon_core.execution.trend_filter import DEFAULT_TREND_FILTER
from beacon_core.execution.planner import DEFAULT_PLANNER
from beacon_core.analysis.sidecar import DEFAULT_ANALYTICS
from beacon_core.analysis.structure import DEFAULT_STRUCTURE
from beacon_core.settings_store import get_setting, set_setting


async def _get_or_create(session, model, defaults=None, **keys):
    row = (await session.execute(select(model).filter_by(**keys))).scalar_one_or_none()
    if row:
        return row, False
    row = model(**keys, **(defaults or {}))
    session.add(row); await session.flush()
    return row, True


async def main():
    await init_models()
    async with Session()() as s:
        # Created disabled and WITHOUT credentials — secrets are entered in the
        # portal and stored encrypted, never read from .env.
        broker, _ = await _get_or_create(
            s, Broker, name="Capital Demo",
            defaults=dict(
                type="capital.com", is_demo=True, enabled=False,
                credentials_ref={"is_demo": True}))

        account, _ = await _get_or_create(
            s, Account, broker_id=broker.id, broker_account_id="DEMO-1",
            defaults=dict(
                name="Gold Demo", currency="USD", enabled=True,
                risk_config={"basis": "capital_percent", "value": "1.0",
                             "allocation": "per_tp",
                             "per_tp_percent": {"1": "4.0", "2": "2.0", "3": "1.5"}}))

        await _get_or_create(
            s, SymbolMap, broker_id=broker.id, internal_symbol="XAUUSD",
            defaults=dict(
                broker_epic="GOLD",
                # CALIBRATE per broker: money per 1.0 price move per 1.0 size.
                value_per_point=Decimal("1"),
                min_lot=Decimal("0.01"), lot_step=Decimal("0.01"),
                min_stop_distance=Decimal("0.5")))

        # A telegram source (disabled for trading until you confirm channel_id).
        await _get_or_create(
            s, Source, kind="telegram", name="Sample Gold Channel",
            defaults=dict(
                external_id="-1000000000000", enabled_for_trading=False,
                is_trusted=True,
                strategy={"entry_ttl_minutes": 60,
                          "sl_rules": [
                              {"trigger": {"type": "tp_hit", "index": 1},
                               "action": {"type": "move_sl_to", "target": "entry"}},
                              {"trigger": {"type": "tp_hit", "index": 2},
                               "action": {"type": "move_sl_to", "target": "previous_tp"}}]},
                risk_config={},                # inherit account risk_config
                account_map=[account.id]))

        # A manual/API desk you can POST to immediately (enabled).
        await _get_or_create(
            s, Source, kind="manual", name="Manual Desk",
            defaults=dict(
                external_id="manual-desk-key", enabled_for_trading=True,
                is_trusted=True,
                strategy={"entry_ttl_minutes": 60,
                          "sl_rules": [
                              {"trigger": {"type": "tp_hit", "index": 1},
                               "action": {"type": "move_sl_to", "target": "entry"}}]},
                risk_config={"basis": "capital_percent", "value": "1.0",
                             "allocation": "even"},
                account_map=[account.id]))

        # Seed the risk-limit brakes ON by default (never trade with no caps),
        # and the global default SL ratchet ladder. Only if not already set, so
        # re-running never clobbers operator tuning.
        if not (await get_setting(s, "risk_limits", None)):
            await set_setting(s, "risk_limits", dict(DEFAULT_RISK_LIMITS))
        strat_cfg = dict(await get_setting(s, "strategy", {}) or {})
        strat_cfg.setdefault("default_sl_rules", DEFAULT_SL_RULES)
        await set_setting(s, "strategy", strat_cfg)

        # Trend-alignment entry filter (#48) — ENABLED as the #72 rollout on demo:
        # counter-trend entries held 93% of the ledger loss (2026-07-13, n=135)
        # while aligned entries were ~breakeven, and 07-13 confirmed it prospectively.
        # setdefault so re-seeding never clobbers an operator who later tunes or
        # disables it — fully reversible from the Risk page (PUT /entry-filters).
        # The library DEFAULT stays off (opt-in A/B contract); this is the
        # deployment's initial rollout value, not a standing directional bias.
        ef_cfg = dict(await get_setting(s, "entry_filters", {}) or {})
        ef_cfg.setdefault("trend_alignment", {**DEFAULT_TREND_FILTER, "enabled": True})
        await set_setting(s, "entry_filters", ef_cfg)

        # Entry/planner config (#67) — the market-on-receipt chase guard etc.
        # Seeded so it's visible/editable from the Risk page; safe defaults apply
        # even if unset (a MARKET-hint entry never chases beyond the tolerance).
        pl_cfg = dict(await get_setting(s, "planner", {}) or {})
        for k, v in DEFAULT_PLANNER.items():
            pl_cfg.setdefault(k, v)
        await set_setting(s, "planner", pl_cfg)

        # Shadow analytics sidecar (#51/#52) — pure observability, off the hot
        # path, on by default. Set analytics.enabled=false to disable entirely.
        an_cfg = dict(await get_setting(s, "analytics", {}) or {})
        for k, v in DEFAULT_ANALYTICS.items():
            an_cfg.setdefault(k, v)
        await set_setting(s, "analytics", an_cfg)

        # Persistent structure/magnet map (#61) — shadow observability, weekly
        # recompute. The nested `filter` block is DISABLED (Phase-3 scaffolding).
        st_cfg = dict(await get_setting(s, "structure", {}) or {})
        for k, v in DEFAULT_STRUCTURE.items():
            st_cfg.setdefault(k, v)
        await set_setting(s, "structure", st_cfg)

        await s.commit()
    print("seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
