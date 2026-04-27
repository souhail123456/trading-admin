"""
Alpaca Broker Adapter
---------------------
Handles all broker communication via Alpaca API.
Supports both paper and live trading (controlled by ALPACA_BASE_URL).

Provides:
  - submit_order(): place market/limit orders
  - get_positions(): current holdings
  - get_account(): portfolio value, buying power
  - close_position(): sell a position
  - get_bars(): real-time/recent price bars

Env vars required:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
  ALPACA_BASE_URL  (default: paper trading)

Usage:
    from pipeline.agents.broker_alpaca import AlpacaBroker
    broker = AlpacaBroker()
    broker.submit_order("XLE", qty=100, side="buy")
"""

import logging
import os
from datetime import datetime

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Paper trading by default
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class AlpacaBroker:
    def __init__(self):
        self.api_key = os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        self.base_url = os.environ.get("ALPACA_BASE_URL", PAPER_URL)
        self.data_url = DATA_URL

        if not self.api_key or not self.secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

        self.is_paper = "paper" in self.base_url
        log.info(f"Alpaca broker initialized ({'PAPER' if self.is_paper else 'LIVE'})")

    def _request(self, method: str, url: str, **kwargs) -> dict | list:
        resp = requests.request(method, url, headers=self.headers, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    # ----- Account -----

    def get_account(self) -> dict:
        """Get account info: equity, buying power, etc."""
        data = self._request("GET", f"{self.base_url}/v2/account")
        return {
            "equity": float(data["equity"]),
            "buying_power": float(data["buying_power"]),
            "cash": float(data["cash"]),
            "portfolio_value": float(data["portfolio_value"]),
            "status": data["status"],
            "pattern_day_trader": data.get("pattern_day_trader", False),
        }

    # ----- Orders -----

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str = "buy",
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> dict:
        """
        Submit an order. Returns order dict with id, status, filled_price.
        """
        payload = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }

        if limit_price and order_type in ("limit", "stop_limit"):
            payload["limit_price"] = str(round(limit_price, 2))
        if stop_price and order_type in ("stop", "stop_limit"):
            payload["stop_price"] = str(round(stop_price, 2))

        log.info(f"Submitting order: {side.upper()} {int(qty)} {symbol} ({order_type})")

        data = self._request("POST", f"{self.base_url}/v2/orders", json=payload)
        return {
            "order_id": data["id"],
            "symbol": data["symbol"],
            "qty": float(data["qty"]),
            "side": data["side"],
            "type": data["type"],
            "status": data["status"],
            "submitted_at": data["submitted_at"],
            "filled_avg_price": float(data["filled_avg_price"]) if data.get("filled_avg_price") else None,
        }

    def get_order(self, order_id: str) -> dict:
        """Get order status by ID."""
        data = self._request("GET", f"{self.base_url}/v2/orders/{order_id}")
        return {
            "order_id": data["id"],
            "symbol": data["symbol"],
            "qty": float(data["qty"]),
            "side": data["side"],
            "status": data["status"],
            "filled_avg_price": float(data["filled_avg_price"]) if data.get("filled_avg_price") else None,
            "filled_qty": float(data["filled_qty"]) if data.get("filled_qty") else 0,
        }

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"{self.base_url}/v2/orders/{order_id}")

    # ----- Positions -----

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        data = self._request("GET", f"{self.base_url}/v2/positions")
        return [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "side": p["side"],
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": float(p["unrealized_plpc"]),
            }
            for p in data
        ]

    def get_position(self, symbol: str) -> dict | None:
        """Get position for a specific symbol."""
        try:
            data = self._request("GET", f"{self.base_url}/v2/positions/{symbol}")
            return {
                "symbol": data["symbol"],
                "qty": float(data["qty"]),
                "avg_entry_price": float(data["avg_entry_price"]),
                "current_price": float(data["current_price"]),
                "unrealized_pl": float(data["unrealized_pl"]),
            }
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def close_position(self, symbol: str) -> dict:
        """Close entire position for a symbol (market sell)."""
        log.info(f"Closing position: {symbol}")
        data = self._request("DELETE", f"{self.base_url}/v2/positions/{symbol}")
        return {
            "order_id": data.get("id"),
            "symbol": symbol,
            "status": data.get("status", "submitted"),
        }

    def close_all_positions(self) -> list:
        """Emergency: close all positions."""
        log.warning("CLOSING ALL POSITIONS")
        return self._request("DELETE", f"{self.base_url}/v2/positions")

    # ----- Market Data -----

    def get_latest_price(self, symbol: str) -> float | None:
        """Get latest trade price for a symbol."""
        try:
            data = self._request(
                "GET",
                f"{self.data_url}/v2/stocks/{symbol}/trades/latest",
            )
            return float(data["trade"]["p"])
        except Exception as e:
            log.warning(f"Failed to get price for {symbol}: {e}")
            return None

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Get latest prices for multiple symbols."""
        prices = {}
        for symbol in symbols:
            price = self.get_latest_price(symbol)
            if price:
                prices[symbol] = price
        return prices

    def is_market_open(self) -> bool:
        """Check if market is currently open."""
        data = self._request("GET", f"{self.base_url}/v2/clock")
        return data["is_open"]
