#!/usr/bin/env python3
"""
capital_probe.py — poke Capital.com's REST API and print the RAW responses.

Why: a working order and its resulting position have DIFFERENT dealIds. The
adapter normalizes field names away, which hides exactly the linkage we need.
This script shows the real JSON — dealReference, dealId, affectedDeals,
workingOrderData, position, and /history/activity — so we can wire the
order→position correlation from fact instead of guessing.

Credentials: read from the environment or a .env file (never hardcoded).
    CAP_API_KEY, CAP_USERNAME, CAP_PASSWORD
    CAP_DEMO=true   (default; set false only if you know what you're doing)

Install: pip install httpx      (or run inside the api container, httpx is there)

USAGE (read-only probes are safe; write actions need --yes):

    python capital_probe.py session
    python capital_probe.py accounts
    python capital_probe.py market GOLD
    python capital_probe.py positions
    python capital_probe.py orders
    python capital_probe.py activity --days 1
    python capital_probe.py confirm <dealReference>

  Write actions (DEMO strongly recommended):
    python capital_probe.py place-limit GOLD BUY 0.01 --offset -50 --yes
    python capital_probe.py cancel <dealId> --yes
    python capital_probe.py place-market GOLD BUY 0.01 --yes           # opens a position
    python capital_probe.py close <dealId> --yes

  The money shot — demonstrates the id change end to end:
    python capital_probe.py lifecycle GOLD --yes
      places a working order -> confirms it -> shows the working-order dealId,
      then places a market order -> confirms -> shows the NEW position dealId
      (different!) -> dumps /history/activity linking them -> cleans everything up.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional

try:
    import httpx
except ImportError:
    sys.exit("httpx is required:  pip install httpx")

DEMO_BASE = "https://demo-api-capital.backend-capital.com"
LIVE_BASE = "https://api-capital.backend-capital.com"


# ---------------------------------------------------------------- helpers ----
def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def mask(s: str) -> str:
    if not s:
        return "(empty)"
    return s[:3] + "…" + s[-2:] if len(s) > 6 else "***"


def dump(title: str, obj: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, default=str))


def keyfields(title: str, pairs: dict) -> None:
    print(f"\n--- KEY FIELDS: {title} ---")
    for k, v in pairs.items():
        print(f"    {k:24} = {v}")


# ------------------------------------------------------------------ client ----
class Capital:
    def __init__(self) -> None:
        self.api_key = os.environ.get("CAP_API_KEY", "")
        self.username = os.environ.get("CAP_USERNAME", "")
        self.password = os.environ.get("CAP_PASSWORD", "")
        demo = os.environ.get("CAP_DEMO", "true").lower() not in ("false", "0", "no")
        self.base = DEMO_BASE if demo else LIVE_BASE
        self.demo = demo
        self.cst: Optional[str] = None
        self.xst: Optional[str] = None
        self._c = httpx.Client(base_url=self.base, timeout=30.0)
        if not (self.api_key and self.username and self.password):
            sys.exit("Missing CAP_API_KEY / CAP_USERNAME / CAP_PASSWORD "
                     "(env or .env).")

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        if self.cst:
            h["CST"] = self.cst
        if self.xst:
            h["X-SECURITY-TOKEN"] = self.xst
        if extra:
            h.update(extra)
        return h

    def login(self, max_retries: int = 4) -> dict:
        print(f"Base URL : {self.base}  ({'DEMO' if self.demo else 'LIVE'})")
        print(f"API key  : {mask(self.api_key)}")
        print(f"Username : {self.username}")
        start_wait = int(os.environ.get("CAP_START_WAIT", "0"))
        if start_wait:
            print(f"  waiting {start_wait}s before first attempt (letting the "
                  f"throttle window clear)…")
            time.sleep(start_wait)
        delay = 30
        for attempt in range(1, max_retries + 1):
            r = self._c.post("/api/v1/session",
                             headers=self._headers(),
                             json={"identifier": self.username, "password": self.password})
            if r.status_code == 200:
                break
            if r.status_code == 429:
                # Session-creation throttle, per API KEY. If the Beacon stack is
                # already down and you still see this, another app (e.g. Beacon
                # Screener's live WebSocket) is using the SAME key. Give it its
                # own key, or idle everything ~5 min. NOTE: each attempt here can
                # re-extend the window, so we retry slowly and few times.
                if attempt == max_retries:
                    break
                print(f"  429 too-many-requests — attempt {attempt}/{max_retries}, "
                      f"waiting {delay}s. If the stack is already down, another "
                      f"app is sharing this API key (see Beacon Screener).")
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            dump("SESSION ERROR", {"status": r.status_code, "body": r.text})
            sys.exit("Login failed. Check credentials and demo/live mode.")
        else:
            r = None
        if r is None or r.status_code != 200:
            sys.exit("Still rate-limited. Stop EVERY app using this API key "
                     "(including Beacon Screener), leave it idle ~5 minutes with "
                     "no probe runs, then try once. Better: create a separate "
                     "Capital.com API key just for Beacon Trader.")
        self.cst = r.headers.get("CST")
        self.xst = r.headers.get("X-SECURITY-TOKEN")
        keyfields("session tokens", {"CST": mask(self.cst or ""),
                                     "X-SECURITY-TOKEN": mask(self.xst or "")})
        return r.json()

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        time.sleep(0.15)
        r = self._c.get(path, headers=self._headers(), params=params)
        r.raise_for_status()
        return r.json() if r.content else {}

    def post(self, path: str, body: dict) -> dict:
        time.sleep(0.2)
        r = self._c.post(path, headers=self._headers(), json=body)
        if r.status_code >= 400:
            dump(f"POST {path} ERROR", {"status": r.status_code, "body": r.text})
            r.raise_for_status()
        return r.json() if r.content else {}

    def delete(self, path: str) -> dict:
        time.sleep(0.2)
        r = self._c.request("DELETE", path, headers=self._headers())
        if r.status_code >= 400:
            dump(f"DELETE {path} ERROR", {"status": r.status_code, "body": r.text})
            r.raise_for_status()
        return r.json() if r.content else {}


# ---------------------------------------------------------------- commands ----
def cmd_session(cap: Capital, args):
    cap.login()
    accts = cap.get("/api/v1/accounts")
    dump("GET /accounts", accts)
    for a in accts.get("accounts", []):
        keyfields("account", {"accountId": a.get("accountId"),
                              "accountName": a.get("accountName"),
                              "currency": (a.get("balance") or {}).get("currency") or a.get("currency"),
                              "balance": (a.get("balance") or {}).get("balance"),
                              "preferred": a.get("preferred")})


def cmd_accounts(cap: Capital, args):
    cap.login()
    dump("GET /accounts", cap.get("/api/v1/accounts"))


def cmd_market(cap: Capital, args):
    cap.login()
    data = cap.get(f"/api/v1/markets/{args.epic}")
    dump(f"GET /markets/{args.epic}", data)
    instr = data.get("instrument", {}) or {}
    dealing = data.get("dealingRules", {}) or {}
    snap = data.get("snapshot", {}) or {}
    keyfields("instrument", {
        "epic": instr.get("epic"),
        "currency": instr.get("currency"),
        "type": instr.get("type"),
        "lotSize": instr.get("lotSize"),
        "contractSize": instr.get("contractSize"),
        "minDealSize": (dealing.get("minDealSize") or {}),
        "minStepDistance": (dealing.get("minStepDistance") or {}),
        "bid/offer": f"{snap.get('bid')} / {snap.get('offer')}",
    })


def cmd_positions(cap: Capital, args):
    cap.login()
    data = cap.get("/api/v1/positions")
    dump("GET /positions", data)
    for p in data.get("positions", []):
        pos = p.get("position", {}) or {}
        mkt = p.get("market", {}) or {}
        keyfields("position", {"dealId": pos.get("dealId"),
                               "dealReference": pos.get("dealReference"),
                               "epic": mkt.get("epic"),
                               "direction": pos.get("direction"),
                               "size": pos.get("size"),
                               "level": pos.get("level"),
                               "stopLevel": pos.get("stopLevel"),
                               "profitLevel": pos.get("profitLevel")})


def cmd_orders(cap: Capital, args):
    cap.login()
    data = cap.get("/api/v1/workingorders")
    dump("GET /workingorders", data)
    for o in data.get("workingOrders", []):
        wod = o.get("workingOrderData", {}) or {}
        mkt = o.get("market", {}) or {}
        keyfields("working order", {"dealId": wod.get("dealId"),
                                    "epic": mkt.get("epic") or wod.get("epic"),
                                    "direction": wod.get("direction"),
                                    "orderType": wod.get("orderType"),
                                    "orderSize": wod.get("orderSize"),
                                    "orderLevel": wod.get("orderLevel"),
                                    "createdDate": wod.get("createdDate")})


def cmd_activity(cap: Capital, args):
    cap.login()
    import datetime as dt
    frm = (dt.datetime.utcnow() - dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    data = cap.get("/api/v1/history/activity", params={"from": frm, "detailed": "true"})
    dump(f"GET /history/activity (last {args.days}d)", data)
    for a in data.get("activities", [])[:20]:
        keyfields("activity", {"dealId": a.get("dealId"),
                               "epic": a.get("epic"),
                               "type": a.get("type"),
                               "status": a.get("status"),
                               "description": a.get("description")})


def cmd_confirm(cap: Capital, args):
    cap.login()
    data = cap.get(f"/api/v1/confirms/{args.deal_reference}")
    dump(f"GET /confirms/{args.deal_reference}", data)
    keyfields("confirm", {"dealReference": data.get("dealReference"),
                          "dealId": data.get("dealId"),
                          "dealStatus": data.get("dealStatus"),
                          "status": data.get("status"),
                          "reason": data.get("reason"),
                          "affectedDeals": data.get("affectedDeals")})


def _require_yes(args, what: str):
    if not args.yes:
        sys.exit(f"Refusing to {what} without --yes. (Use a DEMO account.)")


def cmd_place_limit(cap: Capital, args):
    _require_yes(args, "place a working order")
    cap.login()
    # rest the limit far from price so it doesn't fill — we just want to see the
    # working-order shape and its dealId.
    mkt = cap.get(f"/api/v1/markets/{args.epic}")
    snap = mkt.get("snapshot", {})
    ref_price = float(snap.get("offer") or snap.get("bid") or 0)
    level = round(ref_price + args.offset, 2)
    body = {"epic": args.epic, "direction": args.direction.upper(), "size": args.size,
            "type": "LIMIT", "level": level}
    print(f"Placing LIMIT {args.direction} {args.size} {args.epic} @ {level} "
          f"(market ~{ref_price})")
    res = cap.post("/api/v1/workingorders", body)
    dump("POST /workingorders", res)
    ref = res.get("dealReference")
    if ref:
        time.sleep(0.5)
        conf = cap.get(f"/api/v1/confirms/{ref}")
        dump("GET /confirms/{ref}", conf)
        keyfields("confirm -> working order", {
            "dealReference": conf.get("dealReference"),
            "dealId (working order)": conf.get("dealId"),
            "dealStatus": conf.get("dealStatus"),
            "affectedDeals": conf.get("affectedDeals")})
        print("\nInspect it:   python capital_probe.py orders")
        print(f"Cancel it:    python capital_probe.py cancel {conf.get('dealId')} --yes")


def cmd_place_market(cap: Capital, args):
    _require_yes(args, "open a market position")
    cap.login()
    body = {"epic": args.epic, "direction": args.direction.upper(), "size": args.size}
    print(f"Opening MARKET {args.direction} {args.size} {args.epic}")
    res = cap.post("/api/v1/positions", body)
    dump("POST /positions", res)
    ref = res.get("dealReference")
    if ref:
        time.sleep(0.6)
        conf = cap.get(f"/api/v1/confirms/{ref}")
        dump(f"GET /confirms/{ref}", conf)
        pos_id = None
        for d in conf.get("affectedDeals", []) or []:
            if d.get("status") in ("OPENED", "FULLY_CLOSED", "PARTIALLY_CLOSED"):
                pos_id = d.get("dealId")
        keyfields("confirm -> position", {
            "dealReference": conf.get("dealReference"),
            "dealId (deal)": conf.get("dealId"),
            "affectedDeals": conf.get("affectedDeals"),
            "==> POSITION dealId": pos_id})
        if pos_id:
            print(f"\nClose it:   python capital_probe.py close {pos_id} --yes")


def cmd_cancel(cap: Capital, args):
    _require_yes(args, "cancel an order")
    cap.login()
    dump(f"DELETE /workingorders/{args.deal_id}",
         cap.delete(f"/api/v1/workingorders/{args.deal_id}"))


def cmd_close(cap: Capital, args):
    _require_yes(args, "close a position")
    cap.login()
    dump(f"DELETE /positions/{args.deal_id}",
         cap.delete(f"/api/v1/positions/{args.deal_id}"))


def cmd_lifecycle(cap: Capital, args):
    """The full demonstration: order dealId != position dealId, with cleanup."""
    _require_yes(args, "run the lifecycle (places & closes real demo trades)")
    cap.login()
    epic = args.epic

    mkt = cap.get(f"/api/v1/markets/{epic}")
    snap = mkt.get("snapshot", {})
    price = float(snap.get("offer") or snap.get("bid") or 0)
    size = args.size
    print(f"\nMarket {epic} ~ {price}. Using size {size}.")

    # 1) Working order (won't fill): capture its dealId
    print("\n########## STEP 1: working order (resting, unfilled) ##########")
    lvl = round(price - 50, 2)
    wo = cap.post("/api/v1/workingorders",
                  {"epic": epic, "direction": "BUY", "size": size, "type": "LIMIT", "level": lvl})
    dump("POST /workingorders", wo)
    wo_ref = wo.get("dealReference")
    time.sleep(0.6)
    wo_conf = cap.get(f"/api/v1/confirms/{wo_ref}")
    dump("confirm (working order)", wo_conf)
    wo_deal_id = wo_conf.get("dealId")
    keyfields("STEP1 result", {"dealReference": wo_ref, "working order dealId": wo_deal_id})

    # 2) Market order: capture the NEW position dealId
    print("\n########## STEP 2: market order (opens a position) ##########")
    mo = cap.post("/api/v1/positions", {"epic": epic, "direction": "BUY", "size": size})
    dump("POST /positions", mo)
    mo_ref = mo.get("dealReference")
    time.sleep(0.6)
    mo_conf = cap.get(f"/api/v1/confirms/{mo_ref}")
    dump("confirm (market order)", mo_conf)
    pos_id = None
    for d in mo_conf.get("affectedDeals", []) or []:
        if d.get("status") == "OPENED":
            pos_id = d.get("dealId")
    keyfields("STEP2 result", {"dealReference": mo_ref,
                               "confirm dealId": mo_conf.get("dealId"),
                               "POSITION dealId (affectedDeals)": pos_id})

    # 3) Show them side by side + the activity trail
    print("\n########## STEP 3: the point ##########")
    keyfields("id comparison", {
        "working order dealId": wo_deal_id,
        "position dealId": pos_id,
        "same?": wo_deal_id == pos_id})

    import datetime as dt
    frm = (dt.datetime.utcnow() - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
    act = cap.get("/api/v1/history/activity", params={"from": frm, "detailed": "true"})
    dump("GET /history/activity (last 10m) — the order↔position trail", act)

    # 4) Cleanup
    print("\n########## STEP 4: cleanup ##########")
    if wo_deal_id:
        try:
            dump("cancel working order", cap.delete(f"/api/v1/workingorders/{wo_deal_id}"))
        except Exception as e:
            print("cancel failed:", e)
    if pos_id:
        try:
            dump("close position", cap.delete(f"/api/v1/positions/{pos_id}"))
        except Exception as e:
            print("close failed:", e)
    print("\nDone. Paste the STEP1/STEP2/STEP3 output and the /history/activity "
          "block back, and the correlation can be wired exactly.")


def main():
    load_dotenv(os.environ.get("BEACON_ENV", ".env"))
    p = argparse.ArgumentParser(description="Capital.com raw API probe")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("session").set_defaults(fn=cmd_session)
    sub.add_parser("accounts").set_defaults(fn=cmd_accounts)

    m = sub.add_parser("market"); m.add_argument("epic", nargs="?", default="GOLD")
    m.set_defaults(fn=cmd_market)

    sub.add_parser("positions").set_defaults(fn=cmd_positions)
    sub.add_parser("orders").set_defaults(fn=cmd_orders)

    a = sub.add_parser("activity"); a.add_argument("--days", type=int, default=1)
    a.set_defaults(fn=cmd_activity)

    c = sub.add_parser("confirm"); c.add_argument("deal_reference")
    c.set_defaults(fn=cmd_confirm)

    pl = sub.add_parser("place-limit")
    pl.add_argument("epic"); pl.add_argument("direction"); pl.add_argument("size", type=float)
    pl.add_argument("--offset", type=float, default=-50.0, help="level offset from market")
    pl.add_argument("--yes", action="store_true"); pl.set_defaults(fn=cmd_place_limit)

    pm = sub.add_parser("place-market")
    pm.add_argument("epic"); pm.add_argument("direction"); pm.add_argument("size", type=float)
    pm.add_argument("--yes", action="store_true"); pm.set_defaults(fn=cmd_place_market)

    cn = sub.add_parser("cancel"); cn.add_argument("deal_id")
    cn.add_argument("--yes", action="store_true"); cn.set_defaults(fn=cmd_cancel)

    cl = sub.add_parser("close"); cl.add_argument("deal_id")
    cl.add_argument("--yes", action="store_true"); cl.set_defaults(fn=cmd_close)

    lc = sub.add_parser("lifecycle"); lc.add_argument("epic", nargs="?", default="GOLD")
    lc.add_argument("--size", type=float, default=0.01)
    lc.add_argument("--yes", action="store_true"); lc.set_defaults(fn=cmd_lifecycle)

    args = p.parse_args()
    cap = Capital()
    args.fn(cap, args)


if __name__ == "__main__":
    main()
