"""
cTrader Broker Adapter (Fusion Markets)
---------------------------------------
Handles forex execution via cTrader Open API.

Provides:
  - connect(): authenticate with cTrader
  - submit_order(): market/limit/stop orders
  - get_positions(): current holdings
  - get_account(): balance, equity, margin
  - close_position(): close a trade
  - get_symbol_price(): real-time bid/ask

Env vars required:
  CTRADER_CLIENT_ID       — from openapi.ctrader.com
  CTRADER_CLIENT_SECRET   — from openapi.ctrader.com
  CTRADER_ACCESS_TOKEN    — OAuth2 token
  CTRADER_ACCOUNT_ID      — trading account ID (ctid)

Usage:
    from pipeline.agents.broker_ctrader import CTraderBroker
    broker = CTraderBroker()
    broker.connect()
    broker.submit_order("EURUSD", volume=1000, side="buy")  # 0.01 lot = micro
"""

import logging
import os
import time
from threading import Event

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

from twisted.internet import reactor
from twisted.internet.threads import deferToThread
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# cTrader symbol IDs for major FX pairs (Fusion Markets)
# These are standard — may need adjustment per broker
FX_SYMBOLS = {
    "EURUSD": 1,
    "GBPUSD": 2,
    "USDJPY": 3,
    "USDCHF": 4,
    "AUDUSD": 5,
    "USDCAD": 6,
    "NZDUSD": 7,
    "EURGBP": 8,
    "EURJPY": 9,
    "GBPJPY": 10,
}

# Volume units: cTrader uses "cents" (1 lot = 100,000 units = 10,000,000 cents)
# Micro lot = 0.01 lot = 1,000 units = 100,000 cents
MICRO_LOT = 100_000  # in cTrader volume units


class CTraderBroker:
    def __init__(self):
        self.client_id = os.environ.get("CTRADER_CLIENT_ID", "")
        self.client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
        self.access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "")
        self.account_id = int(os.environ.get("CTRADER_ACCOUNT_ID", "0"))

        if not all([self.client_id, self.client_secret, self.access_token, self.account_id]):
            raise ValueError(
                "cTrader env vars required: CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, "
                "CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID"
            )

        self.client = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._connected = Event()
        self._authorized = Event()
        self._account_auth = Event()
        self._last_response = None
        self._response_event = Event()
        self._symbol_map = {}  # symbol_id -> symbol_name

        log.info("cTrader broker initialized (Fusion Markets)")

    def connect(self):
        """Authenticate with cTrader."""
        # Set up callbacks
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)

        # Start reactor in background thread
        self._reactor_thread = threading.Thread(target=self._run_reactor, daemon=True)
        self._reactor_thread.start()

        # Start connection
        self.client.startService()

        # Wait for full auth chain
        if not self._connected.wait(timeout=10):
            raise ConnectionError("Failed to connect to cTrader")
        if not self._authorized.wait(timeout=10):
            raise ConnectionError("Failed to authorize with cTrader")
        if not self._account_auth.wait(timeout=10):
            raise ConnectionError("Failed to authorize account")

        log.info("cTrader fully connected and authorized")

    def _run_reactor(self):
        reactor.run(installSignalHandlers=False)

    def _on_connected(self, client):
        log.info("Connected to cTrader server")
        self._connected.set()
        # Step 1: App authorization
        request = ProtoOAApplicationAuthReq()
        request.clientId = self.client_id
        request.clientSecret = self.client_secret
        self.client.send(request)

    def _on_disconnected(self, client, reason):
        log.warning(f"Disconnected from cTrader: {reason}")
        self._connected.clear()

    def _on_message(self, client, message):
        payload_type = message.payloadType

        if payload_type == ProtoOAApplicationAuthRes().payloadType:
            log.info("App authorized")
            self._authorized.set()
            # Step 2: Account authorization
            request = ProtoOAAccountAuthReq()
            request.ctidTraderAccountId = self.account_id
            request.accessToken = self.access_token
            self.client.send(request)

        elif payload_type == ProtoOAAccountAuthRes().payloadType:
            log.info(f"Account {self.account_id} authorized")
            self._account_auth.set()

        elif payload_type == ProtoOAExecutionEvent().payloadType:
            event = Protobuf.extract(message)
            log.info(f"Execution event: {event.executionType}")
            self._last_response = event
            self._response_event.set()

        elif payload_type == ProtoOATraderRes().payloadType:
            event = Protobuf.extract(message)
            self._last_response = event
            self._response_event.set()

        elif payload_type == ProtoOAReconcileRes().payloadType:
            event = Protobuf.extract(message)
            self._last_response = event
            self._response_event.set()

        elif hasattr(message, 'payloadType'):
            event = Protobuf.extract(message)
            self._last_response = event
            self._response_event.set()

    def _wait_response(self, timeout: float = 10.0):
        """Wait for a response from cTrader."""
        self._response_event.clear()
        self._last_response = None
        if self._response_event.wait(timeout=timeout):
            return self._last_response
        raise TimeoutError("No response from cTrader")

    # ----- Orders -----

    def submit_order(
        self,
        symbol: str,
        volume: int,  # in cTrader units (100,000 = micro lot)
        side: str = "buy",
        order_type: str = "market",
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
    ) -> dict:
        """
        Submit a forex order.
        volume: in cTrader units. Use MICRO_LOT (100,000) for 0.01 lot.
        """
        symbol_clean = symbol.replace("=X", "").replace("/", "")

        request = ProtoOANewOrderReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId = self._get_symbol_id(symbol_clean)
        request.volume = volume
        request.tradeSide = ProtoOATradeSide.BUY if side.lower() == "buy" else ProtoOATradeSide.SELL
        request.orderType = ProtoOAOrderType.MARKET if order_type == "market" else ProtoOAOrderType.LIMIT

        if stop_loss_pips:
            request.relativeStopLoss = int(stop_loss_pips * 10)  # in points
        if take_profit_pips:
            request.relativeTakeProfit = int(take_profit_pips * 10)

        log.info(f"Submitting: {side.upper()} {symbol_clean} vol={volume} "
                 f"(SL={stop_loss_pips}pips, TP={take_profit_pips}pips)")

        self.client.send(request)
        response = self._wait_response()

        return {
            "symbol": symbol_clean,
            "side": side,
            "volume": volume,
            "status": "submitted",
            "response": str(response) if response else "no_response",
        }

    def close_position(self, position_id: int, volume: int | None = None) -> dict:
        """Close a position by ID."""
        request = ProtoOAClosePositionReq()
        request.ctidTraderAccountId = self.account_id
        request.positionId = position_id
        if volume:
            request.volume = volume

        log.info(f"Closing position {position_id}")
        self.client.send(request)
        response = self._wait_response()

        return {
            "position_id": position_id,
            "status": "closed",
            "response": str(response) if response else "no_response",
        }

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        request = ProtoOAReconcileReq()
        request.ctidTraderAccountId = self.account_id

        self.client.send(request)
        response = self._wait_response()

        positions = []
        if response and hasattr(response, 'position'):
            for pos in response.position:
                positions.append({
                    "position_id": pos.positionId,
                    "symbol_id": pos.tradeData.symbolId,
                    "side": "buy" if pos.tradeData.tradeSide == ProtoOATradeSide.BUY else "sell",
                    "volume": pos.tradeData.volume,
                    "entry_price": pos.price,
                    "swap": pos.swap,
                    "commission": pos.commission,
                    "unrealized_pnl": pos.moneyDigits if hasattr(pos, 'moneyDigits') else 0,
                })
        return positions

    def get_account(self) -> dict:
        """Get account balance and equity."""
        request = ProtoOATraderReq()
        request.ctidTraderAccountId = self.account_id

        self.client.send(request)
        response = self._wait_response()

        if response and hasattr(response, 'trader'):
            t = response.trader
            return {
                "balance": t.balance / 100,  # cents to dollars
                "account_id": t.ctidTraderAccountId,
                "leverage": t.leverageInCents / 100 if hasattr(t, 'leverageInCents') else 0,
                "deposit_currency": t.depositAssetId,
            }
        return {"balance": 0, "error": "no response"}

    def _get_symbol_id(self, symbol: str) -> int:
        """Look up cTrader symbol ID. Falls back to hardcoded map."""
        symbol = symbol.upper().replace("/", "")
        if symbol in FX_SYMBOLS:
            return FX_SYMBOLS[symbol]
        raise ValueError(f"Unknown symbol: {symbol}. Known: {list(FX_SYMBOLS.keys())}")

    def disconnect(self):
        """Clean disconnect."""
        try:
            self.client.stopService()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helper: convert our signal format to cTrader volume
# ---------------------------------------------------------------------------

def calc_fx_volume(
    account_balance: float,
    risk_pct: float,
    stop_loss_pips: float,
    pip_value: float = 0.10,  # per micro lot for most USD pairs
) -> int:
    """
    Calculate position size in cTrader volume units.
    Returns volume in cTrader cents (100,000 = 1 micro lot = 0.01 lot).
    """
    risk_amount = account_balance * risk_pct
    micro_lots = risk_amount / (stop_loss_pips * pip_value)
    volume = int(micro_lots * MICRO_LOT)
    return max(volume, MICRO_LOT)  # minimum 1 micro lot
