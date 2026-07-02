"""Capital.com adapter — async, idiomatic.

Wraps the Capital.com REST API per the v1 documentation:
  https://capital.com/en-ae/trading-platforms/api-development-guide
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Dict, List, Optional

import httpx

from .base import BrokerAdapter
from .types import (
    AccountInfo, AuthError, BrokerError, BrokerInstrument, BrokerOrder,
    BrokerPosition, BrokerQuote, ClosePositionResult, Direction, ModifyOrderRequest,
    ModifyPositionRequest, NetworkError, NotFoundError, OrderSide,
    OrderStatus, OrderType, PlaceOrderRequest, RateLimitError, to_dec,
)
from ..logging import get_logger
from datetime import datetime

log = get_logger("capital")


_LIVE_HOST = "api-capital.backend-capital.com"
_DEMO_HOST = "demo-api-capital.backend-capital.com"
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _map_status(capital_status: str) -> OrderStatus:
    s = (capital_status or "").upper()
    if s in ("ACCEPTED", "FILLED", "EXECUTED"):
        return OrderStatus.FILLED
    if s in ("REJECTED", "ERROR"):
        return OrderStatus.REJECTED
    if s in ("CANCELLED", "DELETED"):
        return OrderStatus.CANCELLED
    if s in ("OPEN", "WORKING", "PENDING_OPEN"):
        return OrderStatus.WORKING
    return OrderStatus.PENDING


class CapitalComAdapter(BrokerAdapter):
    is_automated = True

    def __init__(self, credentials=None, display_metadata=None, base_url=None):
        super().__init__(credentials, display_metadata, base_url)
        self._cst: Optional[str] = None
        self._sec_token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._session_lock = asyncio.Lock()

    @property
    def _host(self) -> str:
        if self.base_url:
            return self.base_url
        if bool(self.credentials.get("is_demo")):
            return _DEMO_HOST
        return _LIVE_HOST

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://{self._host}",
                timeout=_DEFAULT_TIMEOUT,
                headers={"User-Agent": "beacon-trader/1.0"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # Capital.com strictly rate-limits the /session (login) endpoint. Retry a
    # 429 a few times with exponential backoff so bursts of activity ride it out
    # instead of failing the whole operation.
    _SESSION_MAX_RETRIES = 4
    _SESSION_BACKOFF_BASE = 1.5   # seconds: 1.5, 3, 6, 12

    async def _ensure_session(self) -> None:
        if self._cst and self._sec_token:
            return
        async with self._session_lock:
            if self._cst and self._sec_token:
                return
            client = await self._get_client()

            last_rate_limit: Optional[RateLimitError] = None
            for attempt in range(self._SESSION_MAX_RETRIES):
                try:
                    resp = await client.post(
                        "/api/v1/session",
                        json={
                            "identifier": self.credentials.get("account_username", ""),
                            "password": self.credentials.get("account_password", ""),
                        },
                        headers={"X-CAP-API-KEY": self.credentials.get("api_key", "")},
                    )
                except httpx.RequestError as exc:
                    raise NetworkError(f"Capital.com unreachable: {exc}") from exc

                if resp.status_code == 429:
                    last_rate_limit = RateLimitError(
                        "Capital.com rate-limited the session call")
                    if attempt < self._SESSION_MAX_RETRIES - 1:
                        await asyncio.sleep(self._SESSION_BACKOFF_BASE * (2 ** attempt))
                        continue
                    raise last_rate_limit

                if resp.status_code == 401:
                    raise AuthError("Capital.com rejected the credentials")
                if resp.status_code >= 400:
                    raise BrokerError(f"Session failed: HTTP {resp.status_code} {resp.text[:200]}")

                cst = resp.headers.get("CST")
                sec = resp.headers.get("X-SECURITY-TOKEN")
                if not cst or not sec:
                    raise AuthError("Capital.com session response missing CST/X-SECURITY-TOKEN")
                self._cst = cst
                self._sec_token = sec
                await self._select_account(client)
                return

    async def _select_account(self, client) -> None:
        """Switch the session to the configured account (accountId).

        Capital.com places every order on the session's *active* account. Without
        this, orders would land on the login's default account regardless of the
        account you mapped in Beacon — so nothing would appear on the account you
        are watching. Best-effort: a bad/absent id leaves the default active."""
        acct = self.credentials.get("account_id")
        if not acct:
            return
        try:
            resp = await client.put(
                "/api/v1/session", json={"accountId": str(acct)},
                headers=self._auth_headers())
            if resp.status_code >= 400:
                log.warning("account switch to %s failed: HTTP %s %s",
                            acct, resp.status_code, resp.text[:150])
                return
            # Some responses rotate the session tokens; keep them in sync.
            cst = resp.headers.get("CST")
            sec = resp.headers.get("X-SECURITY-TOKEN")
            if cst:
                self._cst = cst
            if sec:
                self._sec_token = sec
        except httpx.RequestError as exc:
            log.warning("account switch to %s errored: %s", acct, exc)

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "CST": self._cst or "",
            "X-SECURITY-TOKEN": self._sec_token or "",
            "Content-Type": "application/json",
        }

    async def _request(self, method, path, json=None, params=None, _retry_on_401=True):
        await self._ensure_session()
        client = await self._get_client()
        try:
            resp = await client.request(method, path, json=json, params=params, headers=self._auth_headers())
        except httpx.RequestError as exc:
            raise NetworkError(f"Capital.com network error: {exc}") from exc

        if resp.status_code == 401 and _retry_on_401:
            self._cst = None
            self._sec_token = None
            return await self._request(method, path, json=json, params=params, _retry_on_401=False)
        if resp.status_code == 404:
            raise NotFoundError(f"Capital.com 404 on {path}")
        if resp.status_code == 429:
            raise RateLimitError("Capital.com rate-limited")
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            raise BrokerError(f"Capital.com {resp.status_code}: {detail}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def healthcheck(self) -> dict:
        try:
            info = await self.get_account_info()
            return {"ok": True, "message": f"connected as {info.account_id}",
                    "currency": info.currency,
                    "balance": str(info.balance) if info.balance is not None else None}
        except AuthError as e:
            return {"ok": False, "message": f"auth failed: {e}"}
        except BrokerError as e:
            return {"ok": False, "message": str(e)}

    async def get_account_info(self) -> AccountInfo:
        data = await self._request("GET", "/api/v1/accounts")
        accounts = data.get("accounts") or []
        if not accounts:
            raise NotFoundError("Capital.com returned no accounts")
        # Report the mapped account (equity used for sizing must match the
        # account we actually trade on), else the preferred/first one.
        target = self.credentials.get("account_id")
        a = None
        if target:
            a = next((x for x in accounts if str(x.get("accountId")) == str(target)), None)
        if a is None:
            a = next((x for x in accounts if x.get("preferred")), accounts[0])
        bal = a.get("balance") or {}
        return AccountInfo(
            account_id=str(a.get("accountId") or ""),
            balance=to_dec(bal.get("balance")),
            available=to_dec(bal.get("available")),
            currency=bal.get("currency") or a.get("currency"),
            raw=a,
        )

    async def list_accounts(self) -> List[dict]:
        """All accounts on the login (for the 'add account' picker)."""
        data = await self._request("GET", "/api/v1/accounts")
        out = []
        for a in data.get("accounts") or []:
            bal = a.get("balance") or {}
            out.append({
                "broker_account_id": str(a.get("accountId") or ""),
                "name": a.get("accountName") or a.get("accountId") or "",
                "currency": bal.get("currency") or a.get("currency") or "USD",
                "balance": str(to_dec(bal.get("balance")) or ""),
                "available": str(to_dec(bal.get("available")) or ""),
                "preferred": bool(a.get("preferred")),
                "status": a.get("status"),
            })
        return out

    async def list_positions(self) -> List[BrokerPosition]:
        data = await self._request("GET", "/api/v1/positions")
        out: List[BrokerPosition] = []
        for p in data.get("positions", []):
            pos = p.get("position") or {}
            mkt = p.get("market") or {}
            direction = (pos.get("direction") or "BUY").upper()
            # Parse createdDateUTC if present — Capital.com returns it in a
            # few different fields across endpoints. Defensive: try several.
            opened_raw = (
                pos.get("createdDateUTC")
                or pos.get("createdDate")
                or pos.get("openDateTime")
            )
            opened_at = None
            if opened_raw:
                try:
                    # Strip trailing Z then parse — accept both with and without.
                    opened_at = datetime.fromisoformat(opened_raw.replace("Z", "+00:00"))
                    # We store as naive UTC for consistency with the rest of the DB.
                    if opened_at.tzinfo is not None:
                        opened_at = opened_at.replace(tzinfo=None)
                except (ValueError, AttributeError):
                    opened_at = None
            out.append(BrokerPosition(
                broker_symbol=str(mkt.get("epic") or pos.get("epic") or ""),
                broker_position_ref=str(pos.get("dealId") or ""),
                quantity=to_dec(pos.get("size")) or Decimal("0"),
                avg_open_price=to_dec(pos.get("level")),
                current_price=to_dec(mkt.get("bid") if direction == "BUY" else mkt.get("offer")),
                unrealized_pl=to_dec(pos.get("upl") or pos.get("profit")),
                unrealized_pl_pct=None,
                stop_loss=to_dec(pos.get("stopLevel")),
                take_profit=to_dec(pos.get("profitLevel")),
                opened_at=opened_at,
                currency=pos.get("currency") or mkt.get("currency"),
                direction=Direction.LONG if direction == "BUY" else Direction.SHORT,
                raw=p,
            ))
        return out

    async def list_orders(self, status: Optional[OrderStatus] = None) -> List[BrokerOrder]:
        data = await self._request("GET", "/api/v1/workingorders")
        out: List[BrokerOrder] = []
        for w in data.get("workingOrders", []):
            wo = w.get("workingOrderData") or {}
            mkt = w.get("marketData") or {}
            side = OrderSide.BUY if (wo.get("direction") or "").upper() == "BUY" else OrderSide.SELL
            ot_raw = (wo.get("orderType") or "LIMIT").upper()
            ot = OrderType.LIMIT if ot_raw == "LIMIT" else (OrderType.STOP if ot_raw == "STOP" else OrderType.MARKET)
            out.append(BrokerOrder(
                broker_order_ref=str(wo.get("dealId") or ""),
                broker_symbol=str(wo.get("epic") or mkt.get("epic") or ""),
                side=side, order_type=ot,
                quantity=to_dec(wo.get("orderSize")) or Decimal("0"),
                limit_price=to_dec(wo.get("orderLevel")),
                stop_loss=to_dec(wo.get("stopLevel")),
                take_profit=to_dec(wo.get("limitLevel")),
                status=OrderStatus.WORKING,
                currency=wo.get("currencyCode") or mkt.get("currency"),
                raw=w,
            ))
        if status is not None:
            out = [o for o in out if o.status == status]
        return out

    async def place_order(self, req: PlaceOrderRequest) -> BrokerOrder:
        if req.order_type == OrderType.MARKET:
            payload = {"epic": req.broker_symbol, "direction": req.side.value, "size": float(req.quantity)}
            if req.stop_loss is not None: payload["stopLevel"] = float(req.stop_loss)
            if req.take_profit is not None: payload["profitLevel"] = float(req.take_profit)
            data = await self._request("POST", "/api/v1/positions", json=payload)
        else:
            ot = "LIMIT" if req.order_type == OrderType.LIMIT else "STOP"
            level = req.limit_price
            if level is None:
                raise BrokerError("limit_price is required for LIMIT/STOP orders")
            payload = {
                "epic": req.broker_symbol, "direction": req.side.value, "size": float(req.quantity),
                "type": ot, "level": float(level),
            }
            if req.stop_loss is not None: payload["stopLevel"] = float(req.stop_loss)
            if req.take_profit is not None: payload["profitLevel"] = float(req.take_profit)
            data = await self._request("POST", "/api/v1/workingorders", json=payload)

        deal_ref = data.get("dealReference")
        if not deal_ref:
            raise BrokerError(f"Capital.com place_order missing dealReference: {data}")

        confirm = await self._request("GET", f"/api/v1/confirms/{deal_ref}")

        # dealStatus is the authority on accept/reject (ACCEPTED | REJECTED).
        # `status` is the resulting deal state (OPEN/…) and must NOT be used to
        # infer acceptance. `reason` explains a rejection (e.g. MARKET_CLOSED,
        # RISK_CHECK, INSUFFICIENT_FUNDS, MIN_SIZE, invalid epic).
        deal_status = (confirm.get("dealStatus") or "").upper()
        reason = confirm.get("reason")
        affected = confirm.get("affectedDeals") or []

        if deal_status and deal_status != "ACCEPTED":
            return BrokerOrder(
                broker_order_ref=str(confirm.get("dealId") or deal_ref),
                broker_symbol=str(confirm.get("epic") or req.broker_symbol),
                side=req.side, order_type=req.order_type,
                quantity=req.quantity, limit_price=req.limit_price,
                stop_loss=req.stop_loss, take_profit=req.take_profit,
                status=OrderStatus.REJECTED,
                rejection_reason=reason or confirm.get("status") or "rejected",
                currency=confirm.get("currency"), raw=confirm,
            )

        if req.order_type == OrderType.MARKET:
            # The opened position carries its OWN dealId in affectedDeals; that
            # is the id GET /positions lists, so the monitor can reconcile it.
            opened = next(
                (d.get("dealId") for d in affected
                 if (d.get("status") or "").upper() in ("OPENED", "OPEN", "FULLY_CLOSED")),
                None,
            )
            ref = str(opened or confirm.get("dealId") or deal_ref)
            status = OrderStatus.FILLED
            fill_price = to_dec(confirm.get("level"))
            fill_qty = to_dec(confirm.get("size")) or req.quantity
        else:
            # A resting working order: its dealId is what GET /workingorders lists.
            ref = str(confirm.get("dealId") or deal_ref)
            status = OrderStatus.WORKING
            fill_price = None
            fill_qty = None

        return BrokerOrder(
            broker_order_ref=ref,
            broker_symbol=str(confirm.get("epic") or req.broker_symbol),
            side=req.side, order_type=req.order_type,
            quantity=to_dec(confirm.get("size")) or req.quantity,
            limit_price=req.limit_price,
            stop_loss=to_dec(confirm.get("stopLevel") or req.stop_loss),
            take_profit=to_dec(confirm.get("profitLevel") or req.take_profit),
            status=status,
            fill_price=fill_price,
            fill_quantity=fill_qty,
            currency=confirm.get("currency"),
            raw=confirm,
        )

    async def cancel_order(self, broker_order_ref: str) -> bool:
        try:
            data = await self._request("DELETE", f"/api/v1/workingorders/{broker_order_ref}")
            ref = data.get("dealReference")
            if not ref:
                return False
            confirm = await self._request("GET", f"/api/v1/confirms/{ref}")
            return _map_status(confirm.get("status")) == OrderStatus.CANCELLED
        except NotFoundError:
            return False

    async def modify_position(self, req: ModifyPositionRequest) -> BrokerPosition:
        """PUT /api/v1/positions/{dealId} — update SL/TP on an open position.

        Capital.com semantics: the body MUST include the levels you want to
        keep. Omitting stopLevel/profitLevel does NOT preserve them — it
        clears them. So we fetch the current position first, then merge
        in the requested changes. This mirrors what the legacy bot did
        (capital_core.move_stop_loss_to_entry preserved profitLevel).
        """
        # 1) Fetch current state to preserve fields the caller left as None.
        # Capital.com returns a single-position payload at /positions/{dealId}.
        try:
            current = await self._request("GET", f"/api/v1/positions/{req.broker_position_ref}")
        except NotFoundError:
            raise NotFoundError(f"Position {req.broker_position_ref} not found")
        cur_pos = (current.get("position") or {})

        body: Dict[str, float] = {}
        # stopLevel: use new value if provided, else preserve existing.
        new_sl = req.stop_loss if req.stop_loss is not None else to_dec(cur_pos.get("stopLevel"))
        new_tp = req.take_profit if req.take_profit is not None else to_dec(cur_pos.get("profitLevel"))
        if new_sl is not None:
            body["stopLevel"] = float(new_sl)
        if new_tp is not None:
            body["profitLevel"] = float(new_tp)
        if not body:
            raise BrokerError("modify_position needs at least one of stop_loss / take_profit")

        data = await self._request("PUT", f"/api/v1/positions/{req.broker_position_ref}", json=body)
        deal_ref = data.get("dealReference")
        if not deal_ref:
            raise BrokerError(f"Capital.com modify_position missing dealReference: {data}")

        # Confirm and return the post-modify state. /confirms doesn't always
        # report SL/TP, so we re-read /positions/{ref} for the canonical view.
        try:
            await self._request("GET", f"/api/v1/confirms/{deal_ref}")
        except NotFoundError:
            # Capital.com occasionally serves the confirm 404 right after a
            # successful modify; the position itself reflects the change.
            pass
        # Re-list to pick up the now-current SL/TP. Cheap; one round trip.
        for p in await self.list_positions():
            if p.broker_position_ref == req.broker_position_ref:
                return p
        # If the position disappeared between PUT and re-list it was probably
        # closed; raise NotFound rather than returning a fabricated row.
        raise NotFoundError(
            f"Position {req.broker_position_ref} disappeared after modify"
        )

    async def close_position(
        self, broker_position_ref: str, quantity: Optional[Decimal] = None,
    ) -> ClosePositionResult:
        """DELETE /api/v1/positions/{dealId} — close a position by ref.

        Capital.com's DELETE always closes the FULL position. Partial close
        requires opening an opposing position of the requested size (kludgy
        but documented). For Milestone 4 we only support full close; partial
        close needs more design (does the closed bit follow the original
        position's SL/TP? does the remainder?).
        """
        if quantity is not None:
            raise BrokerError(
                "Partial close not yet implemented for Capital.com — "
                "the broker requires opening an opposing position, which "
                "introduces P&L attribution questions we haven't designed."
            )
        try:
            data = await self._request("DELETE", f"/api/v1/positions/{broker_position_ref}")
        except NotFoundError:
            return ClosePositionResult(
                broker_position_ref=broker_position_ref,
                closed=False,
                raw={"reason": "position not found"},
            )

        deal_ref = data.get("dealReference")
        if not deal_ref:
            return ClosePositionResult(
                broker_position_ref=broker_position_ref,
                closed=False,
                raw=data,
            )
        try:
            confirm = await self._request("GET", f"/api/v1/confirms/{deal_ref}")
        except NotFoundError:
            confirm = {}
        ok = _map_status(confirm.get("status") or confirm.get("dealStatus")) in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,  # both mean "no longer open"
        ) or confirm.get("dealStatus") == "ACCEPTED"
        return ClosePositionResult(
            broker_position_ref=broker_position_ref,
            closed=ok,
            closed_quantity=to_dec(confirm.get("size")),
            close_price=to_dec(confirm.get("level")),
            realized_pl=to_dec(confirm.get("profit")),
            raw=confirm,
        )

    async def modify_order(self, req: ModifyOrderRequest) -> BrokerOrder:
        """PUT /api/v1/workingorders/{dealId} — change levels on a working order.

        Same preserve-then-merge pattern as modify_position: omitted fields
        get cleared by the broker, so we read first.
        """
        # Working orders aren't fetched by id one-by-one — list and find.
        try:
            data = await self._request("GET", "/api/v1/workingorders")
        except NotFoundError:
            raise NotFoundError(f"Working order {req.broker_order_ref} not found")
        target = None
        for w in data.get("workingOrders", []):
            wo = w.get("workingOrderData") or {}
            if str(wo.get("dealId") or "") == req.broker_order_ref:
                target = wo
                break
        if target is None:
            raise NotFoundError(f"Working order {req.broker_order_ref} not found")

        body: Dict[str, float] = {}
        new_level = req.limit_price if req.limit_price is not None else to_dec(target.get("orderLevel"))
        new_sl    = req.stop_loss   if req.stop_loss   is not None else to_dec(target.get("stopLevel"))
        new_tp    = req.take_profit if req.take_profit is not None else to_dec(target.get("profitLevel"))
        if new_level is not None: body["level"] = float(new_level)
        if new_sl is not None:    body["stopLevel"] = float(new_sl)
        if new_tp is not None:    body["profitLevel"] = float(new_tp)
        if not body:
            raise BrokerError("modify_order needs at least one of limit_price / stop_loss / take_profit")

        result = await self._request("PUT", f"/api/v1/workingorders/{req.broker_order_ref}", json=body)
        deal_ref = result.get("dealReference")
        if not deal_ref:
            raise BrokerError(f"Capital.com modify_order missing dealReference: {result}")
        try:
            confirm = await self._request("GET", f"/api/v1/confirms/{deal_ref}")
        except NotFoundError:
            confirm = {}
        side_str = (target.get("direction") or "BUY").upper()
        ot_raw = (target.get("orderType") or "LIMIT").upper()
        ot = OrderType.LIMIT if ot_raw == "LIMIT" else (OrderType.STOP if ot_raw == "STOP" else OrderType.MARKET)
        return BrokerOrder(
            broker_order_ref=req.broker_order_ref,
            broker_symbol=str(target.get("epic") or ""),
            side=OrderSide.BUY if side_str == "BUY" else OrderSide.SELL,
            order_type=ot,
            quantity=to_dec(target.get("orderSize")) or Decimal("0"),
            limit_price=new_level,
            stop_loss=new_sl,
            take_profit=new_tp,
            status=_map_status(confirm.get("status")) or OrderStatus.WORKING,
            currency=target.get("currency"),
            raw=confirm,
        )

    async def search_instrument(self, query: str) -> List[BrokerInstrument]:
        data = await self._request("GET", "/api/v1/markets", params={"searchTerm": query})
        out: List[BrokerInstrument] = []
        for m in data.get("markets", []):
            out.append(BrokerInstrument(
                broker_symbol=str(m.get("epic") or ""),
                name=str(m.get("instrumentName") or m.get("epic") or ""),
                instrument_type=m.get("instrumentType"),
                currency=m.get("currency"),
                min_qty=to_dec(m.get("minDealSize")),
            ))
        return out

    async def get_quote(self, broker_symbol: str) -> BrokerQuote:
        """Live quote for one Capital.com epic.

        GET /api/v1/markets/{epic} returns 'instrument' (metadata) and
        'snapshot' (the live block). We pull both into a BrokerQuote.
        """
        if not broker_symbol:
            raise BrokerError("broker_symbol (epic) is required")
        data = await self._request("GET", f"/api/v1/markets/{broker_symbol}")
        snap = data.get("snapshot") or {}
        instr = data.get("instrument") or {}

        bid = to_dec(snap.get("bid"))
        offer = to_dec(snap.get("offer"))
        # Mid is a reasonable 'last' for spot markets when no last-trade is given.
        last = None
        if bid is not None and offer is not None:
            last = (bid + offer) / Decimal(2)

        # Capital.com gives netChange (absolute) and percentageChange. Derive
        # the previous close from last - netChange when both are present.
        net_change = to_dec(snap.get("netChange"))
        prev_close = None
        if last is not None and net_change is not None:
            prev_close = last - net_change

        return BrokerQuote(
            broker_symbol=broker_symbol,
            bid=bid, offer=offer, last_price=last,
            high_price=to_dec(snap.get("high")),
            low_price=to_dec(snap.get("low")),
            close_price=prev_close,
            change_abs=net_change,
            change_pct=to_dec(snap.get("percentageChange")),
            currency=instr.get("currency"),
            market_status=snap.get("marketStatus"),
            raw=data,
        )

    # ---- Historical bars (for the on-demand chart) -------------------------
    # Capital.com's GET /api/v1/prices/{epic} supports:
    #   resolution = MINUTE | MINUTE_5 | MINUTE_15 | MINUTE_30
    #              | HOUR    | HOUR_4
    #              | DAY     | WEEK     | MONTH
    #   from / to  = ISO timestamps (YYYY-MM-DDTHH:MM:SS) — both optional
    #   max        = number of bars (default 10, max 1000)
    #
    # Each price has open/close/high/low as {bid, ask} pairs. We return mid
    # prices, matching what get_quote() uses for last_price.
    _VALID_RESOLUTIONS = {
        "MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30",
        "HOUR", "HOUR_4",
        "DAY", "WEEK", "MONTH",
    }

    async def get_bars(
        self,
        broker_symbol: str,
        resolution: str = "MINUTE_5",
        *,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        max_bars: int = 200,
    ) -> List[dict]:
        """Fetch OHLC bars for an epic at a given resolution.

        Returns a list of bar dicts shaped for the frontend:
            [{"t": iso_ts, "o": float, "h": float, "l": float, "c": float, "v": int}, ...]
        sorted oldest → newest. Empty list when the API returns no prices.
        Network/auth errors propagate as BrokerError subclasses.
        """
        if not broker_symbol:
            raise BrokerError("broker_symbol (epic) is required")
        if resolution not in self._VALID_RESOLUTIONS:
            raise BrokerError(
                f"Unsupported resolution '{resolution}'. "
                f"Allowed: {sorted(self._VALID_RESOLUTIONS)}"
            )
        # Capital's `max` cap is 1000; clamp here so a buggy caller can't 4xx us.
        max_bars = max(1, min(int(max_bars), 1000))
        params: Dict[str, str] = {"resolution": resolution, "max": str(max_bars)}
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts

        data = await self._request("GET", f"/api/v1/prices/{broker_symbol}", params=params)
        prices = data.get("prices") or []

        out: List[dict] = []
        for p in prices:
            # Each leg is {"bid": float, "ask": float}. Mid = (bid+ask)/2.
            def _mid(leg: Optional[dict]) -> Optional[float]:
                if not isinstance(leg, dict):
                    return None
                bid, ask = leg.get("bid"), leg.get("ask")
                if bid is None and ask is None:
                    return None
                if bid is None:
                    return float(ask)
                if ask is None:
                    return float(bid)
                return (float(bid) + float(ask)) / 2.0

            o = _mid(p.get("openPrice"))
            h = _mid(p.get("highPrice"))
            l = _mid(p.get("lowPrice"))
            c = _mid(p.get("closePrice"))
            # Drop bars where we couldn't recover an open or close — they're
            # useless for charting and would confuse client-side rendering.
            if o is None or c is None:
                continue
            out.append({
                "t": p.get("snapshotTime") or p.get("snapshotTimeUTC"),
                "o": o, "h": h, "l": l, "c": c,
                "v": p.get("lastTradedVolume"),
            })
        # Capital returns newest-last already, but be defensive in case
        # we ever switch the param ordering.
        out.sort(key=lambda b: b["t"] or "")
        return out
