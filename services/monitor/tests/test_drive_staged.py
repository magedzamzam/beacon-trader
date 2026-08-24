"""The monitor's ladder driver (#250).

`_drive_staged` is the platform's ONLY order-placing path outside the executor,
and the one that put 23 unintended positions on DemoC. It cannot be imported in
a bare test environment — `main.py` pulls in aiohttp, a broker adapter and a
database, none of which are what these tests are about — so the checks here are
of two kinds:

  * the decisions that could be lifted out are tested for real, in
    `packages/core/tests/test_ladder.py`, against `ladder.order_kind`;
  * what remains is WIRING, and is asserted against the module source, the same
    way `services/executor/tests/test_fanout_placement.py` does.

A source assertion is weaker than a behavioural one and is used only where a
behavioural one is not available. Each one names the failure it is standing
guard over, so a future edit that breaks it can tell whether the rule still
applies or the test is simply stale.
"""
import re
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"
SRC = MAIN.read_text(encoding="utf-8")
DRIVE = SRC[SRC.index("async def _drive_staged("):]
DRIVE = DRIVE[:DRIVE.index("\nasync def ", 10)]


def test_every_leg_is_placed_with_its_own_order_type():
    """A tranche groups rungs by TRIGGER, not by order type: the shipped 1-zone
    cells from three TPs up put a MARKET rung and a STOP rung on the same `mid`.
    Deriving ONE type from `tr.mode` market-filled the STOP rungs — opening at
    MID the confirmation order that should only fill on a recovery."""
    assert "LAD.order_kind(leg.order_type, tr.mode)" in DRIVE
    # the per-tranche derivation must be gone
    assert not re.search(r"order_type\s*=\s*\(?OrderType\.STOP if tr\.mode", DRIVE)
    assert 'is_market = (tr.mode or "")' not in DRIVE


def test_the_resting_level_comes_from_the_leg_too():
    """Each rung rests at its OWN entry — `mid` owns a MARKET rung at MID and a
    STOP rung back at the entry, two different prices."""
    assert "limit_price=None if is_market else leg.entry" in DRIVE


def test_a_tranche_is_armed_only_if_a_leg_actually_rests_as_a_stop():
    assert 'tr.state = "armed" if armed else "deployed"' in DRIVE
    assert "armed = armed or order_type is OrderType.STOP" in DRIVE


def test_a_rung_that_placed_nothing_is_not_recorded_as_deployed():
    """A deployed tranche is out of `_staged_pending`'s reach, so if every order
    was rejected the trade would wait for ever on orders that do not exist —
    nothing to fill it, and nothing left that could expire it."""
    assert "if not placed:" in DRIVE
    assert "tr.state, tr.state_since = STG.REJECTED, utcnow()" in DRIVE


def test_a_cancel_row_can_leave_pending():
    """A cancel row owns no legs, so it cannot expire alongside them, and the
    rung loop used to skip `cancel:` roles outright. With the fill precondition
    on the other side, a trade where nothing ever filled kept a pending tranche
    for ever: the trade never closed, its planned risk sat in the open-risk caps
    blocking later signals, and the monitor polled the broker for it every tick."""
    rung_loop = DRIVE[DRIVE.index("--- rungs waiting for their level"):]
    assert 'if str(tr.role or "").startswith("cancel:"):' in rung_loop
    assert "ladder: nothing left to cancel" in rung_loop
    # the TTL branch must come BEFORE the `continue` that skips the row
    head = rung_loop[:rung_loop.index('startswith("cancel:")')]
    assert "minutes = (now - tr.state_since)" in head, \
        "the age must be computed before the cancel-row branch can use it"


def test_both_loops_choose_the_predicate_by_trigger_type():
    """A TP sits in the WINNING direction, so the entry-side `reached` is true of
    it from the first tick. Asking it about a TP1 trigger fired the rule
    immediately — which is how signal 1431 cancelled its whole ladder before a
    single rung had filled."""
    assert DRIVE.count("LAD.reached_target if") >= 2
    assert not re.search(r"not LAD\.reached\(direction, price,", DRIVE)


def test_the_decisions_run_on_the_side_that_would_actually_trade():
    """The mid is not a price anyone deals at. On a wide spread it says a level
    was reached while the tradeable side is still short of it."""
    assert "entry_px = float(_q_in)" in DRIVE and "target_px = float(_q_out)" in DRIVE
    assert '_q_in = getattr(quote, "offer" if direction == "BUY" else "bid", None)' in DRIVE
    assert '_q_out = getattr(quote, "bid" if direction == "BUY" else "offer", None)' in DRIVE
    assert "await _drive_staged(session, trade, adapter, smap, quote, mid, ttl_min)" in SRC


def test_the_absolute_entry_age_ceiling_is_checked_before_a_rung_deploys():
    """`max_entry_age_minutes` bounds the age of the whole entry however late a
    rung deploys. It was only ever asked about legs already WORKING, so a pending
    rung sailed past it, opened a position on a setup the operator had declared
    stale, and had that position's own order pulled a tick later."""
    assert "STG.entry_age_exceeded(cfg, entry_age)" in DRIVE
    assert "ladder: max entry age" in DRIVE
    # measured from the signal, not from the tranche's own clock
    assert "entry_age = (now - trade.created_at)" in DRIVE


# --- deployed_risk measures against the PLANNED stop (#264) -------------------
RISK = SRC[SRC.index("async def _record_deployed_risk("):]
RISK = RISK[:RISK.index("\nasync def ", 10)]


def test_deployed_risk_uses_the_legs_planned_stop():
    """`signals.sl` stopped being the planned stop when `sl_override` (#249)
    began replacing it before the plan was built. Measuring a leg sized against
    4648.20 on a stop of 4654.20 read 1.8% low on trade 1391 — and only on
    accounts with `sl_distance` set, which is one of the three A/B arms, so the
    error was biased rather than noisy."""
    assert "leg.initial_sl if leg.initial_sl is not None else sig.sl" in RISK
    assert "_stop(l)" in RISK
    # the two leg sets must agree on the stop, or the ratio is meaningless
    assert RISK.count("_stop(l)") == 2


def test_it_still_refuses_the_ratcheted_stop():
    """The property #188 added `signals.sl` for. `legs.sl` is moved in place by
    the ratchet, so a breakeven trade would report ~0 deployed risk."""
    assert "l.sl)" not in RISK, "legs.sl must never be the measurement stop"


def test_the_executor_stamps_the_planned_stop_when_the_leg_is_made():
    """One construction site, and `pleg.sl` is already post-override there."""
    ex = (MAIN.parent.parent / "executor" / "main.py").read_text(encoding="utf-8")
    assert "sl=pleg.sl, initial_sl=pleg.sl" in ex


# --- orphaned orders: the intent log (#259, monitor site) --------------------
def test_the_intent_is_durable_before_any_order_is_placed():
    """`tick()` commits once at the end for every trade. Between place_order()
    returning and that commit, a real order exists that the database knows
    nothing about — and if the commit fails, the tranche is still `pending` so
    the next tick places the whole rung AGAIN. That is how one varchar overflow
    became 48 working orders and 23 unmanaged positions on 2026-08-24."""
    assert "tr.state, tr.state_since = STG.DEPLOYING, utcnow()" in DRIVE
    intent = DRIVE.index('_commit_now(session, "staged_deploy_intent"')
    first_order = DRIVE.index("await adapter.place_order(")
    assert intent < first_order, "the intent must be committed BEFORE placing"


def test_a_failed_intent_commit_places_nothing():
    """The safe direction to fail in: nothing at the broker, tranche rolled back
    to `pending`, next tick simply retries."""
    tail = DRIVE[DRIVE.index('_commit_now(session, "staged_deploy_intent"'):]
    assert tail[:200].count("continue") == 1


def test_a_deploying_tranche_is_never_driven_again():
    """`_staged_pending` returns only `pending`, so `deploying` is out of the
    drive loop's reach — that is the whole safety property. A duplicated rung is
    money; a stuck one is a phone call."""
    pend = SRC[SRC.index("async def _staged_pending("):]
    pend = pend[:pend.index("\nasync def ", 10)]
    assert 'StagedTranche.state == "pending"' in pend
    assert "DEPLOYING" not in pend


def test_each_placement_is_persisted_as_it_happens():
    assert '_commit_now(session, "staged_leg_placed"' in DRIVE
    # and a leg already carrying a broker ref is never sent twice
    assert "if leg.broker_order_ref or leg.broker_position_ref:" in DRIVE


def test_a_stuck_tranche_is_surfaced_and_not_silently_healed():
    """Guessing which anonymous working order belongs to which rung is how a
    foreign dealId ended up on 506 legs (#234). Alert, don't adopt."""
    assert "staged_deploy_stuck" in DRIVE
    assert "STUCK mid-deploy" in DRIVE
    # above the no-work early return, or the one case it exists for can't reach it
    stuck = DRIVE.index("StagedTranche.state == STG.DEPLOYING")
    early = DRIVE.index("if not pending:\n        return")
    assert stuck < early, "the stuck check must run before the early return"


def test_the_driver_runs_even_with_nothing_pending():
    """A trade whose only surviving tranche is stuck has nothing pending — and is
    exactly the trade whose orphaned orders need surfacing."""
    assert 'if staged_pending or trade.entry_style == "staged":' in SRC
