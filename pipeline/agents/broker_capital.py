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
        """Create a new session (or refresh expired one)."""
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
        if resp.status_code >= 400:
            log.error(f"Capital.com session error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()

        self.security_token = resp.headers.get("X-SECURITY-TOKEN", "")
        self.cst = resp.headers.get("CST", "")
        self._session_time = time.time()
        log.info("Capital.com session created")

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
        resp = requests.request(method, url, headers=self._headers(), timeout=60, **kwargs)
        if resp.status_code >= 400:
            log.error(f"Capital.com API error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json() if resp.text else {}

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
        direction = "BUY" if side.lower() == "buy" else "SELL"

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
