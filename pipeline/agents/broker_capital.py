"""
Capital.com Broker Adapter
--------------------------
REST API for forex execution via Capital.com.

Auth: session-based (email + password + API key → security token + CST).
Sessions expire after 10 min idle; auto-refresh on each request.

Provides:
  - get_account(): balance, equity, open positions count
  - submit_order(): market orders with SL/TP (distance in pips)
  - get_positions(): current holdings with live P&L
  - close_position(): close a deal
  - get_candles(): OHLCV data at any timeframe
  - get_price(): current bid/ask

Env vars required:
  CAPITAL_API_KEY      — from Capital.com settings
  CAPITAL_EMAIL        — login email
  CAPITAL_PASSWORD     — login password
  CAPITAL_ENV          — "demo" (default) or "live"

Usage:
    from pipeline.agents.broker_capital import CapitalBroker
    broker = CapitalBroker()
    broker.submit_order("EURUSD", units=1000, side="buy", stop_loss_pips=50)
"""

import logging
import os
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

DEMO_URL = "https://demo-api-capital.backend-capital.com"
LIVE_URL = "https://api-capital.backend-capital.com"

# Capital.com epic names for FX pairs
FX_EPICS = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
    "USDCHF": "USDCHF",
    "AUDUSD": "AUDUSD",
    "USDCAD": "USDCAD",
    "NZDUSD": "NZDUSD",
    "EURGBP": "EURGBP",
    "EURJPY": "EURJPY",
    "GBPJPY": "GBPJPY",
}


class CapitalBroker:
    def __init__(self):
        self.api_key = os.environ.get("CAPITAL_API_KEY", "")
        self.email = os.environ.get("CAPITAL_EMAIL", "")
        self.password = os.environ.get("CAPITAL_PASSWORD", "")
        self.env = os.environ.get("CAPITAL_ENV", "demo")

        if not self.api_key or not self.email or not self.password:
            raise ValueError("CAPITAL_API_KEY, CAPITAL_EMAIL, and CAPITAL_PASSWORD must be set")

        self.base_url = DEMO_URL if self.env == "demo" else LIVE_URL
        self.security_token = None
        self.cst = None
        self._session_time = 0

        self._create_session()
        log.info(f"Capital.com broker initialized ({self.env})")

    def _create_session(self):
        """Create a new session (or refresh expired one).

        Retries with exponential backoff on transient failures (429 rate-limit,
        5xx, or connection errors). Session auth has no other retry layer — a
        single transient 429 on POST /session used to fail the whole pipeline
        run (observed 2026-08-19 22:31 UTC).
        """
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                resp = requests.post(
                    f"{self.base_url}/api/v1/session",
                    headers={
                        "X-CAP-API-KEY": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "identifier": self.email,
                        "password": self.password,
                    },
                    timeout=10,
                )
                # Retry on rate-limit (429) or transient server errors (5xx)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt < max_attempts - 1:
                        wait = 2 ** attempt  # 1s, 2s, 4s
                        log.warning(
                            f"Capital.com session {resp.status_code} — "
                            f"retry {attempt + 1}/{max_attempts} in {wait}s"
                        )
                        time.sleep(wait)
                        continue
                if resp.status_code >= 400:
                    log.error(f"Capital.com session error {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()

                self.security_token = resp.headers.get("X-SECURITY-TOKEN", "")
                self.cst = resp.headers.get("CST", "")
                self._session_time = time.time()
                log.info("Capital.com session created")
                return
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(
                        f"Capital.com session request error ({e}) — "
                        f"retry {attempt + 1}/{max_attempts} in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    raise
        if last_exc:  # exhausted retries on repeated 429/5xx that raised
            raise last_exc

    def _ensure_session(self):
        """Refresh session if older than 8 minutes (expires at 10)."""
        if time.time() - self._session_time > 480:
            self._create_session()

    def _headers(self) -> dict:
        self._ensure_session()
        return {
            "X-SECURITY-TOKEN": self.security_token,
            "CST": self.cst,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.request(method, url, headers=self._headers(), timeout=60, **kwargs)
                if resp.status_code >= 400:
                    log.error(f"Capital.com API error {resp.status_code}: {resp.text[:300]}")
                # Retry on rate-limit (429) or transient server errors (5xx)
                if attempt < max_retries and (resp.status_code == 429 or 500 <= resp.status_code < 600):
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(f"Capital.com {resp.status_code} — retry {attempt + 1}/{max_retries} in {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json() if resp.text else {}
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(f"Capital.com request error ({e}) — retry {attempt + 1}/{max_retries} in {wait}s")
                    time.sleep(wait)
                else:
                    raise
        raise last_exc  # unreachable, satisfies type checker

    def _epic(self, symbol: str) -> str:
        """Normalize symbol to Capital.com epic."""
        symbol = symbol.upper().replace("=X", "").replace("/", "").replace("_", "")
        return FX_EPICS.get(symbol, symbol)

    # ----- Account -----

    def get_account(self) -> dict:
        data = self._request("GET", "/api/v1/accounts")
        accounts = data.get("accounts", [])
        if not accounts:
            return {}
        acc = accounts[0]
        return {
            "account_id": acc["accountId"],
            "balance": float(acc["balance"]["balance"]),
            "equity": float(acc["balance"]["balance"]) + float(acc["balance"].get("profitLoss", 0)),
            "unrealized_pnl": float(acc["balance"].get("profitLoss", 0)),
            "deposit": float(acc["balance"].get("deposit", 0)),
            "available": float(acc["balance"].get("available", 0)),
            "currency": acc.get("currency", "USD"),
        }

    # ----- Orders -----

    def submit_order(
        self,
        symbol: str,
        units: int,
        side: str = "buy",
        order_type: str = "market",
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
    ) -> dict:
        """
        Submit a market order.
        Capital.com uses 'size' (lots) and 'direction' (BUY/SELL).
        For FX micro lots: size = units / 1000 (1 micro lot = 0.01 standard lot).
        """
        epic = self._epic(symbol)
        direction = "BUY" if side.lower() in ("buy", "long") else "SELL"

        # Capital.com uses raw units (1000 = 1 micro lot)
        # Minimum is 1000 units
        size = max(units, 1000)

        order_body = {
            "epic": epic,
            "direction": direction,
            "size": size,
        }

        is_jpy = "JPY" in epic
        pip_size = 0.01 if is_jpy else 0.0001

        if stop_loss_pips:
            # Capital.com uses distance (absolute value in price units)
            order_body["stopDistance"] = round(stop_loss_pips * pip_size, 5 if not is_jpy else 3)
            order_body["guaranteedStop"] = False

        if take_profit_pips:
            order_body["profitDistance"] = round(take_profit_pips * pip_size, 5 if not is_jpy else 3)

        log.info(f"Submitting: {direction} {size:.2f} lots {epic}")

        data = self._request(
            "POST",
            "/api/v1/positions",
            json=order_body,
        )

        result = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "status": "filled",
        }

        if "dealReference" in data:
            result["deal_reference"] = data["dealReference"]
            # Confirm the deal
            try:
                confirm = self._request("GET", f"/api/v1/confirms/{data['dealReference']}")
                result["deal_id"] = confirm.get("dealId", "")
                result["status"] = confirm.get("dealStatus", "UNKNOWN")
                result["level"] = confirm.get("level")
                result["profit"] = confirm.get("profit")
            except Exception as e:
                log.warning(f"Could not confirm deal: {e}")

        return result

    def close_position(self, deal_id: str) -> dict:
        """Close a position by deal ID."""
        data = self._request(
            "DELETE",
            f"/api/v1/positions/{deal_id}",
        )
        log.info(f"Closed deal: {deal_id}")
        return data

    def partial_close(self, deal_id: str, size: float) -> dict:
        """Partially close a position by deal ID.

        Args:
            deal_id: The deal ID of the open position to partially close.
            size: Number of units to close (not units to keep).
                  E.g. if position is 2000 units and you pass size=1000,
                  1000 units are closed, leaving 1000 open.

        Capital.com supports partial close via DELETE /positions/{dealId}
        with a body containing the size to close.
        """
        size = max(int(size), 1000)  # minimum 1000 units
        data = self._request(
            "DELETE",
            f"/api/v1/positions/{deal_id}",
            json={"size": size},
        )
        log.info(f"Partial close: deal={deal_id}, units_closed={size}")
        return data

    def update_stop(self, deal_id: str, stop_level: float) -> dict:
        """Amend a position's stop-loss level at the broker.

        Uses Capital.com PUT /positions/{dealId} endpoint.
        Returns the confirmation dict or raises on failure.
        """
        body = {"stopLevel": round(stop_level, 5)}
        log.info(f"Updating stop for {deal_id}: stopLevel={stop_level:.5f}")
        data = self._request("PUT", f"/api/v1/positions/{deal_id}", json=body)
        # Confirm the amendment if a dealReference is returned
        if "dealReference" in data:
            try:
                confirm = self._request("GET", f"/api/v1/confirms/{data['dealReference']}")
                log.info(f"Stop update confirmed: {confirm.get('dealStatus', 'UNKNOWN')}")
                return confirm
            except Exception as e:
                log.warning(f"Could not confirm stop update for {deal_id}: {e}")
        return data

    def update_tp(self, deal_id: str, limit_level: float) -> dict:
        """Amend a position's take-profit (limit) level at the broker.

        Uses Capital.com PUT /positions/{dealId} endpoint.
        """
        body = {"limitLevel": round(limit_level, 5)}
        log.info(f"Updating TP for {deal_id}: limitLevel={limit_level:.5f}")
        data = self._request("PUT", f"/api/v1/positions/{deal_id}", json=body)
        if "dealReference" in data:
            try:
                confirm = self._request("GET", f"/api/v1/confirms/{data['dealReference']}")
                log.info(f"TP update confirmed: {confirm.get('dealStatus', 'UNKNOWN')}")
                return confirm
            except Exception as e:
                log.warning(f"Could not confirm TP update for {deal_id}: {e}")
        return data

    def close_all(self) -> list:
        """Close all open positions."""
        positions = self.get_positions()
        results = []
        for p in positions:
            try:
                r = self.close_position(p["deal_id"])
                results.append(r)
            except Exception as e:
                log.error(f"Failed to close {p['epic']}: {e}")
        return results

    # ----- Positions -----

    def get_positions(self) -> list[dict]:
        """Get all open positions with live P&L."""
        data = self._request("GET", "/api/v1/positions")
        positions = []
        for p in data.get("positions", []):
            pos = p.get("position", {})
            market = p.get("market", {})
            positions.append({
                "deal_id": pos.get("dealId", ""),
                "epic": market.get("epic", ""),
                "direction": pos.get("direction", ""),
                "size": float(pos.get("size", 0)),
                "entry_price": float(pos.get("level", 0)),
                "unrealized_pnl": float(pos.get("upl", 0)),
                "stop_level": pos.get("stopLevel"),
                "profit_level": pos.get("limitLevel"),
                "created_date": pos.get("createdDateUTC", ""),
                "currency": pos.get("currency", ""),
            })
        return positions

    # ----- History / real-fill reconciliation -----

    @staticmethod
    def _parse_money(val) -> float | None:
        """Parse a Capital.com money string like 'USD-2.50', '-2.50', or a number."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        import re
        m = re.search(r"-?\d+(?:\.\d+)?", str(val).replace(",", ""))
        return float(m.group()) if m else None

    @staticmethod
    def _norm_name(name: str | None) -> str:
        """Strip an instrument name to bare letters for epic matching (EUR/USD -> EURUSD)."""
        import re
        return re.sub(r"[^A-Z]", "", (name or "").upper())

    def get_deal_history(self, deal_id: str, symbol: str | None = None, since_hours: int = 72) -> dict | None:
        """Find the REAL broker-side close of a deal via the history endpoints.

        A position closed by the broker's own server-side TP/SL/stop (or a
        margin/system close) leaves the local DB unaware of the true fill.
        The current-market price at *detection* time is NOT the fill price and
        can wildly overstate a loss (observed 230-260 pip "losses" past an
        80-pip stop). This reads the broker's own record of what actually
        happened.

        Data sources (both reuse `_request()` session/retry machinery):
          - GET /api/v1/history/activity?from&to&detailed=true
              -> position-lifecycle activities. A close carries a `source`
                 (TP / SL / CLOSE_OUT / DEALER / USER / SYSTEM), a `dateUTC`,
                 and (with detailed=true) a `details` object holding the fill
                 `level` and an `actions[]` list whose `actionType` marks
                 POSITION_CLOSED and whose `affectedDealId`/`dealId` ties back
                 to the original position deal id.
          - GET /api/v1/history/transactions?from&to&type=TRADE
              -> realized ledger rows with `closeLevel` and `profitAndLoss`
                 (a money string, account currency). Matched to the deal by
                 instrument + close-price/time proximity.

        Returns {"close_price": float, "close_time": iso|None,
                 "reason": str, "pnl": float|None} where reason is one of
        broker_tp / broker_stop / broker_closed, or None if no close is found
        (endpoints missing, empty window, or deal not matched) -> caller keeps
        its current-price fallback.
        """
        from datetime import datetime, timedelta, timezone

        if not deal_id:
            return None
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        to = now.strftime("%Y-%m-%dT%H:%M:%S")
        epic = self._epic(symbol) if symbol else None

        # --- 1) Activity: locate the closing activity for this deal ---
        activities: list = []
        try:
            adata = self._request(
                "GET", "/api/v1/history/activity",
                params={"from": frm, "to": to, "detailed": "true"},
            )
            if isinstance(adata, dict):
                activities = adata.get("activities") or []
        except Exception as e:
            log.warning(f"get_deal_history: activity fetch failed: {e}")

        def _refs_deal(act: dict) -> bool:
            if act.get("dealId") == deal_id:
                return True
            det = act.get("details") or {}
            if det.get("dealReference") == deal_id:
                return True
            for a in (det.get("actions") or []):
                if a.get("affectedDealId") == deal_id or a.get("dealId") == deal_id:
                    return True
            return False

        def _close_action(act: dict) -> bool:
            det = act.get("details") or {}
            for a in (det.get("actions") or []):
                if "CLOS" in (a.get("actionType") or "").upper():
                    return True
            return "CLOS" in (act.get("actionType") or "").upper()

        close_act = None
        for act in activities:
            if _refs_deal(act) and _close_action(act):
                close_act = act
                break
        if close_act is None:  # fallback: reference + a close-y source
            for act in activities:
                src = (act.get("source") or "").upper()
                if _refs_deal(act) and src in ("TP", "SL", "CLOSE_OUT", "DEALER", "SYSTEM"):
                    close_act = act
                    break

        close_price = None
        close_time = None
        reason = None
        if close_act is not None:
            det = close_act.get("details") or {}
            lvl = det.get("level")
            if lvl is None:
                for a in (det.get("actions") or []):
                    if a.get("level") is not None:
                        lvl = a.get("level")
                        break
            close_price = float(lvl) if lvl is not None else None
            close_time = close_act.get("dateUTC") or close_act.get("dateUtc") or close_act.get("date")
            src = (close_act.get("source") or "").upper()
            if src == "TP":
                reason = "broker_tp"
            elif src == "SL":
                reason = "broker_stop"
            else:
                reason = "broker_closed"

        # --- 2) Transactions: realized P&L (and close price / reason fallback) ---
        pnl = None
        txns: list = []
        try:
            tdata = self._request(
                "GET", "/api/v1/history/transactions",
                params={"from": frm, "to": to, "type": "TRADE"},
            )
            if isinstance(tdata, dict):
                txns = tdata.get("transactions") or []
        except Exception as e:
            log.warning(f"get_deal_history: transactions fetch failed: {e}")

        # Candidate trade rows: right instrument (if known) with a parseable P&L.
        candidates = []
        for t in txns:
            if (t.get("transactionType") or "").upper() not in ("TRADE", ""):
                continue
            if epic and self._norm_name(t.get("instrumentName")) and epic not in self._norm_name(t.get("instrumentName")):
                continue
            p = self._parse_money(t.get("profitAndLoss"))
            if p is None:
                p = self._parse_money(t.get("size")) if t.get("closeLevel") is not None else None
            candidates.append((t, p))

        best = None
        if candidates:
            if close_price is not None:
                def _dist(t):
                    cl = self._parse_money(t.get("closeLevel"))
                    return abs(cl - close_price) if cl is not None else float("inf")
                best = min(candidates, key=lambda c: _dist(c[0]))
            elif close_time is not None:
                # match by nearest timestamp
                def _parse_dt(s):
                    try:
                        return datetime.fromisoformat(str(s).replace("Z", "").split(".")[0])
                    except Exception:
                        return None
                ct = _parse_dt(close_time)
                if ct is not None:
                    def _real_tdist(c):
                        d = _parse_dt(c[0].get("dateUtc") or c[0].get("dateUTC") or c[0].get("date"))
                        return abs((d - ct).total_seconds()) if d else float("inf")
                    best = min(candidates, key=_real_tdist)
                else:
                    best = candidates[0]
            else:
                best = candidates[0]

        if best is not None:
            trow, tp = best
            pnl = tp
            # Fill in close price / time / reason from the ledger row if activity lacked them.
            if close_price is None:
                close_price = self._parse_money(trow.get("closeLevel"))
            if close_time is None:
                close_time = trow.get("dateUtc") or trow.get("dateUTC") or trow.get("date")
            if reason is None:
                note = (trow.get("note") or "").lower()
                if "stop" in note:
                    reason = "broker_stop"
                elif "profit" in note or "take" in note or "limit" in note:
                    reason = "broker_tp"
                else:
                    reason = "broker_closed"

        if close_price is None and pnl is None:
            return None
        if close_price is None:
            # No usable exit price -> let caller fall back to current-price estimate.
            return None
        return {
            "close_price": float(close_price),
            "close_time": close_time,
            "reason": reason or "broker_closed",
            "pnl": pnl,
        }

    # ----- Market Data -----

    def get_price(self, symbol: str) -> dict | None:
        """Get current bid/ask price."""
        epic = self._epic(symbol)
        try:
            data = self._request("GET", f"/api/v1/markets/{epic}")
            snapshot = data.get("snapshot", {})
            bid = float(snapshot.get("bid", 0))
            offer = float(snapshot.get("offer", 0))
            return {
                "bid": bid,
                "ask": offer,
                "spread": round(offer - bid, 6),
                "status": snapshot.get("marketStatus", ""),
                "tradeable": snapshot.get("marketStatus") == "TRADEABLE",
            }
        except Exception as e:
            log.warning(f"Failed to get price for {symbol}: {e}")
        return None

    def get_candles(
        self,
        symbol: str,
        granularity: str = "HOUR_4",  # MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
        count: int = 200,
    ) -> list[dict]:
        """
        Get OHLCV candles.
        Granularity: MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
        """
        epic = self._epic(symbol)

        # Capital.com uses 'resolution' param
        # Map common formats
        resolution_map = {
            "M1": "MINUTE", "M5": "MINUTE_5", "M15": "MINUTE_15", "M30": "MINUTE_30",
            "H1": "HOUR", "H4": "HOUR_4", "D": "DAY", "W": "WEEK",
        }
        resolution = resolution_map.get(granularity, granularity)

        data = self._request(
            "GET",
            f"/api/v1/prices/{epic}",
            params={
                "resolution": resolution,
                "max": min(count, 1000),
            },
        )

        candles = []
        for c in data.get("prices", []):
            mid_open = (float(c["openPrice"]["bid"]) + float(c["openPrice"]["ask"])) / 2
            mid_high = (float(c["highPrice"]["bid"]) + float(c["highPrice"]["ask"])) / 2
            mid_low = (float(c["lowPrice"]["bid"]) + float(c["lowPrice"]["ask"])) / 2
            mid_close = (float(c["closePrice"]["bid"]) + float(c["closePrice"]["ask"])) / 2
            candles.append({
                "time": c.get("snapshotTimeUTC", c.get("snapshotTime", "")),
                "open": round(mid_open, 6),
                "high": round(mid_high, 6),
                "low": round(mid_low, 6),
                "close": round(mid_close, 6),
                "volume": int(c.get("lastTradedVolume", 0)),
            })
        return candles

    def is_market_open(self) -> bool:
        """Check if forex market is currently open."""
        price = self.get_price("EURUSD")
        if price:
            return price["tradeable"]
        return False

    def disconnect(self):
        """Close session."""
        try:
            requests.delete(
                f"{self.base_url}/api/v1/session",
                headers=self._headers(),
                timeout=5,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Diagnostic CLI: verify get_deal_history against a real recently-closed deal.
#   python3 -m pipeline.agents.broker_capital --deal-history <dealId> [--symbol EURUSD] [--hours 168] [--raw]
# Kept as a clean, harmless read-only flag for future re-verification.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json as _json
    from datetime import datetime, timedelta, timezone

    parser = argparse.ArgumentParser(description="Capital.com broker diagnostics")
    parser.add_argument("--deal-history", metavar="DEAL_ID",
                        help="Look up the real close of a deal via history endpoints")
    parser.add_argument("--symbol", help="Symbol hint for the deal (e.g. EURUSD)")
    parser.add_argument("--hours", type=int, default=72, help="Lookback window in hours")
    parser.add_argument("--raw", action="store_true",
                        help="Also dump raw activity/transactions JSON for field verification")
    args = parser.parse_args()

    broker = CapitalBroker()
    try:
        if args.deal_history:
            if args.raw:
                now = datetime.now(timezone.utc)
                frm = (now - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%S")
                to = now.strftime("%Y-%m-%dT%H:%M:%S")
                print("=== RAW activity (detailed) ===")
                act = broker._request("GET", "/api/v1/history/activity",
                                      params={"from": frm, "to": to, "detailed": "true"})
                print(_json.dumps(act, indent=2)[:6000])
                print("=== RAW transactions (TRADE) ===")
                txn = broker._request("GET", "/api/v1/history/transactions",
                                      params={"from": frm, "to": to, "type": "TRADE"})
                print(_json.dumps(txn, indent=2)[:6000])
            print("=== PARSED get_deal_history ===")
            result = broker.get_deal_history(args.deal_history, symbol=args.symbol, since_hours=args.hours)
            print(_json.dumps(result, indent=2))
        else:
            parser.print_help()
    finally:
        broker.disconnect()
