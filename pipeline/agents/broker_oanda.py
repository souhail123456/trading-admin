"""
OANDA Broker Adapter
--------------------
REST API for forex execution via OANDA.
Simple HTTP calls — works from anywhere (GitHub Actions, local, server).

Provides:
  - get_account(): balance, equity, open positions count
  - submit_order(): market/limit/stop orders with SL/TP
  - get_positions(): current holdings with live P&L
  - close_position(): close a trade
  - get_candles(): OHLCV data at any timeframe (M1 to M monthly)
  - get_price(): current bid/ask

Env vars required:
  OANDA_API_KEY        — from OANDA dashboard
  OANDA_ACCOUNT_ID     — practice or live account ID
  OANDA_ENV            — "practice" (default) or "live"

Usage:
    from pipeline.agents.broker_oanda import OandaBroker
    broker = OandaBroker()
    broker.submit_order("EUR_USD", units=1000, side="buy", stop_loss_pips=50)
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

PRACTICE_URL = "https://api-fxpractice.oanda.com"
LIVE_URL = "https://api-fxtrade.oanda.com"

# OANDA instrument names (underscore format)
FX_INSTRUMENTS = {
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
    "USDCHF": "USD_CHF",
    "AUDUSD": "AUD_USD",
    "USDCAD": "USD_CAD",
    "NZDUSD": "NZD_USD",
    "EURGBP": "EUR_GBP",
    "EURJPY": "EUR_JPY",
    "GBPJPY": "GBP_JPY",
}


class OandaBroker:
    def __init__(self):
        self.api_key = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.env = os.environ.get("OANDA_ENV", "practice")

        if not self.api_key or not self.account_id:
            raise ValueError("OANDA_API_KEY and OANDA_ACCOUNT_ID must be set")

        self.base_url = PRACTICE_URL if self.env == "practice" else LIVE_URL
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        log.info(f"OANDA broker initialized ({self.env})")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self.headers, timeout=10, **kwargs)
        if resp.status_code >= 400:
            log.error(f"OANDA API error {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def _instrument(self, symbol: str) -> str:
        """Convert clean symbol to OANDA format: EURUSD -> EUR_USD"""
        symbol = symbol.upper().replace("=X", "").replace("/", "")
        return FX_INSTRUMENTS.get(symbol, symbol)

    # ----- Account -----

    def get_account(self) -> dict:
        data = self._request("GET", f"/v3/accounts/{self.account_id}/summary")
        acc = data["account"]
        return {
            "balance": float(acc["balance"]),
            "equity": float(acc["NAV"]),
            "unrealized_pnl": float(acc["unrealizedPL"]),
            "margin_used": float(acc["marginUsed"]),
            "margin_available": float(acc["marginAvailable"]),
            "open_trade_count": int(acc["openTradeCount"]),
            "currency": acc["currency"],
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
        Submit an order.
        units: positive for buy, negative for sell (OANDA convention).
        For convenience, pass positive units + side="sell" and we negate.
        """
        instrument = self._instrument(symbol)

        if side.lower() == "sell":
            units = -abs(units)
        else:
            units = abs(units)

        order_body = {
            "type": "MARKET" if order_type == "market" else "LIMIT",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",  # Fill or Kill for market orders
        }

        # Get current price for SL/TP calculation
        if stop_loss_pips or take_profit_pips:
            price_data = self.get_price(symbol)
            if price_data:
                current_price = price_data["ask"] if units > 0 else price_data["bid"]
                is_jpy = "JPY" in instrument
                pip_size = 0.01 if is_jpy else 0.0001

                if stop_loss_pips:
                    if units > 0:
                        sl_price = current_price - (stop_loss_pips * pip_size)
                    else:
                        sl_price = current_price + (stop_loss_pips * pip_size)
                    order_body["stopLossOnFill"] = {
                        "price": f"{sl_price:.5f}" if not is_jpy else f"{sl_price:.3f}"
                    }

                if take_profit_pips:
                    if units > 0:
                        tp_price = current_price + (take_profit_pips * pip_size)
                    else:
                        tp_price = current_price - (take_profit_pips * pip_size)
                    order_body["takeProfitOnFill"] = {
                        "price": f"{tp_price:.5f}" if not is_jpy else f"{tp_price:.3f}"
                    }

        log.info(f"Submitting: {side.upper()} {abs(units)} {instrument}")

        data = self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json={"order": order_body},
        )

        result = {
            "instrument": instrument,
            "units": units,
            "side": side,
            "status": "filled",
        }

        if "orderFillTransaction" in data:
            fill = data["orderFillTransaction"]
            result["trade_id"] = fill.get("tradeOpened", {}).get("tradeID")
            result["price"] = float(fill.get("price", 0))
            result["pl"] = float(fill.get("pl", 0))
            result["time"] = fill.get("time")

        return result

    def close_position(self, symbol: str = None, trade_id: str = None) -> dict:
        """Close a position by instrument or trade ID."""
        if trade_id:
            data = self._request(
                "PUT",
                f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
            )
        elif symbol:
            instrument = self._instrument(symbol)
            data = self._request(
                "PUT",
                f"/v3/accounts/{self.account_id}/positions/{instrument}/close",
                json={"longUnits": "ALL"},
            )
        else:
            raise ValueError("Provide symbol or trade_id")

        log.info(f"Closed: {symbol or trade_id}")
        return data

    def close_all(self) -> list:
        """Close all open positions."""
        positions = self.get_positions()
        results = []
        for p in positions:
            try:
                r = self.close_position(trade_id=p["trade_id"])
                results.append(r)
            except Exception as e:
                log.error(f"Failed to close {p['instrument']}: {e}")
        return results

    # ----- Positions -----

    def get_positions(self) -> list[dict]:
        """Get all open trades with live P&L."""
        data = self._request("GET", f"/v3/accounts/{self.account_id}/openTrades")
        trades = []
        for t in data.get("trades", []):
            trades.append({
                "trade_id": t["id"],
                "instrument": t["instrument"],
                "side": "buy" if int(t["currentUnits"]) > 0 else "sell",
                "units": abs(int(t["currentUnits"])),
                "entry_price": float(t["price"]),
                "unrealized_pnl": float(t["unrealizedPL"]),
                "margin_used": float(t.get("marginUsed", 0)),
                "open_time": t["openTime"],
                "stop_loss": float(t["stopLossOrder"]["price"]) if t.get("stopLossOrder") else None,
                "take_profit": float(t["takeProfitOrder"]["price"]) if t.get("takeProfitOrder") else None,
            })
        return trades

    # ----- Market Data -----

    def get_price(self, symbol: str) -> dict | None:
        """Get current bid/ask price."""
        instrument = self._instrument(symbol)
        try:
            data = self._request(
                "GET",
                f"/v3/accounts/{self.account_id}/pricing",
                params={"instruments": instrument},
            )
            prices = data.get("prices", [])
            if prices:
                p = prices[0]
                return {
                    "bid": float(p["bids"][0]["price"]),
                    "ask": float(p["asks"][0]["price"]),
                    "spread": float(p["asks"][0]["price"]) - float(p["bids"][0]["price"]),
                    "time": p["time"],
                    "tradeable": p["tradeable"],
                }
        except Exception as e:
            log.warning(f"Failed to get price for {symbol}: {e}")
        return None

    def get_candles(
        self,
        symbol: str,
        granularity: str = "H4",  # M1, M5, M15, M30, H1, H4, D, W, M
        count: int = 200,
    ) -> list[dict]:
        """
        Get OHLCV candles at any timeframe.
        Granularity: M1, M5, M15, M30, H1, H4, D, W, M
        """
        instrument = self._instrument(symbol)
        data = self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params={
                "granularity": granularity,
                "count": count,
                "price": "M",  # mid prices
            },
        )

        candles = []
        for c in data.get("candles", []):
            if c["complete"]:
                mid = c["mid"]
                candles.append({
                    "time": c["time"],
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c["volume"]),
                })
        return candles

    def is_market_open(self) -> bool:
        """Check if forex market is currently open."""
        price = self.get_price("EURUSD")
        if price:
            return price["tradeable"]
        return False
