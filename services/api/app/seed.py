"""Idempotent seed: a demo Capital.com broker (credentials via .env), one
account with a default risk config, an XAUUSD symbol map, and two sample
sources (a telegram channel + a manual desk) wired with SL
rules, and per-TP risk. Safe to run repeatedly.

    docker compose run --rm api python -m app.seed
"""
import asyncio
from decimal import Decimal

from sqlalchemy import select

from beacon_core.db.base import Session, init_models
from beacon_core.db.models import Account, Broker, Source, SymbolMap


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
        broker, _ = await _get_or_create(
            s, Broker, name="Capital Demo",
            defaults=dict(
                type="capital.com", is_demo=True, enabled=True,
                credentials_ref={
                    "api_key_env": "CAP_API_KEY",
                    "account_username_env": "CAP_USERNAME",
                    "account_password_env": "CAP_PASSWORD",
                    "is_demo": True,
                }))

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
                strategy={"order_position_type": "MARKET",
                          "entry_ttl_minutes": 60,
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
                strategy={"order_position_type": "MARKET",
                          "entry_ttl_minutes": 60,
                          "sl_rules": [
                              {"trigger": {"type": "tp_hit", "index": 1},
                               "action": {"type": "move_sl_to", "target": "entry"}}]},
                risk_config={"basis": "capital_percent", "value": "1.0",
                             "allocation": "even"},
                account_map=[account.id]))
        await s.commit()
    print("seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
