"""
FX Pipeline
-----------
Unified runner for the forex trading pipeline:
  1. Generate FX signals (daily trend + price action)
  2. Risk management (position sizing)
  3. Execute on broker (Capital.com / OANDA / cTrader)
  4. Monitor open positions (time stops, SL management)
  5. Portfolio status + Telegram alert

Execution matches backtested rules:
  - Trend (ID 100): hold while above SMA-200, monthly rebalance, no fixed SL/TP
  - Price Action (ID 101): max 15-day hold, 3% stop loss, exit on bearish pattern

Usage:
    python3 -m pipeline.agents.fx_pipeline --daily
    python3 -m pipeline.agents.fx_pipeline --daily --dry-run
    python3 -m pipeline.agents.fx_pipeline --status
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pipeline.db import init_db, log_agent_action, get_strategy_params

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Risk parameters
ACCOUNT_BALANCE = float(os.environ.get("FX_ACCOUNT_BALANCE", "10000"))
LEVERAGE = float(os.environ.get("FX_LEVERAGE", "500"))
MAX_RISK_PER_TRADE = 0.02  # 2% per trade
MAX_POSITIONS = 3

# Strategy IDs
TREND_STRATEGY_ID = 100
PA_STRATEGY_ID = 101

# Defaults (overridden by DB parameters)
_DEFAULT_TREND_PARAMS = {"stop_loss_pips": 80, "max_hold_days": None, "stop_loss_pct": None}
_DEFAULT_PA_PARAMS = {"stop_loss_pips": 40, "max_hold_days": 15, "stop_loss_pct": 0.03}

# Safety net: trend positions without explicit max_hold get closed after this many days
TREND_SAFETY_MAX_HOLD = 90

# Regime-aware limits
RANGING_MAX_TREND_POSITIONS = 2  # allow 2 trend positions in ranging (was 1 — too restrictive, bot never traded)
VOLATILE_ATR_MULTIPLIER = 1.5   # tighter stops in volatile (vs 2.0 normal)
NORMAL_ATR_MULTIPLIER = 2.0
RANGING_MIN_COMPOSITE_SCORE = 0.05  # composite is ~0.03-0.15 scale (strength*0.7 + carry/10*0.3)

# ATR cache (session-level, avoids repeated yfinance calls)
_atr_cache: dict[str, float | None] = {}


def get_current_regime(conn: sqlite3.Connection) -> dict | None:
    """Load the latest regime classification from global_state.json or agent_log.

    Returns dict with keys: regime, vix, recommendations, updated.
    """
    # Try global_state.json first (written by run_daily step 1)
    state_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "global_state.json")
    try:
        if os.path.exists(state_path):
            with open(state_path) as f:
                data = json.load(f)
            if data.get("regime"):
                return data
    except Exception:
        pass

    # Fallback: most recent regime entry from agent_log
    try:
        row = conn.execute(
            "SELECT outputs FROM agent_log WHERE agent = 'regime_detector' AND action = 'regime_classified' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        pass

    return None


def get_atr(pair: str, period: int = 14) -> float | None:
    """Fetch ATR(period) for a currency pair using yfinance daily data.

    Returns ATR in price terms (e.g. 0.0080 for EURUSD = 80 pips).
    Caches results for the session to avoid repeated API calls.
    """
    if pair in _atr_cache:
        return _atr_cache[pair]

    try:
        import yfinance as yf
        import pandas as pd

        ticker = f"{pair[:3]}{pair[3:]}=X"
        df = yf.download(ticker, period="1mo", interval="1d", progress=False)
        if df is None or len(df) < period + 1:
            _atr_cache[pair] = None
            return None

        high = df["High"].values.flatten()
        low = df["Low"].values.flatten()
        close = df["Close"].values.flatten()

        # True Range calculation
        tr = [high[0] - low[0]]
        for i in range(1, len(high)):
            tr.append(max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            ))

        # Simple moving average of TR for ATR
        atr_val = float(sum(tr[-period:]) / period)
        _atr_cache[pair] = atr_val
        log.info(f"  [ATR] {pair}: ATR({period}) = {atr_val:.5f}")
        return atr_val
    except Exception as e:
        log.warning(f"  [ATR] {pair}: failed to compute ATR: {e}")
        _atr_cache[pair] = None
        return None


def _get_broker():
    """Get broker: Capital.com (preferred) > OANDA > cTrader > None."""
    if os.environ.get("CAPITAL_API_KEY") and os.environ.get("CAPITAL_EMAIL"):
        from pipeline.agents.broker_capital import CapitalBroker
        return CapitalBroker()
    if os.environ.get("OANDA_API_KEY") and os.environ.get("OANDA_ACCOUNT_ID"):
        from pipeline.agents.broker_oanda import OandaBroker
        return OandaBroker()
    if os.environ.get("CTRADER_CLIENT_ID") and os.environ.get("CTRADER_ACCESS_TOKEN"):
        from pipeline.agents.broker_ctrader import CTraderBroker
        broker = CTraderBroker()
        broker.connect()
        return broker
    return None


def _close_broker_position(broker, symbol: str, broker_order_id: str | None):
    """Close a position on the broker."""
    if not broker or not broker_order_id:
        return
    try:
        from pipeline.agents.broker_capital import CapitalBroker
        if isinstance(broker, CapitalBroker):
            broker.close_position(broker_order_id)
        else:
            broker.close_position(symbol=symbol, trade_id=broker_order_id)
        log.info(f"    Broker position closed: {symbol} ({broker_order_id})")
    except Exception as e:
        log.error(f"    Failed to close broker position {symbol}: {e}")


# ---------------------------------------------------------------------------
# Monitor: check open positions for time/stop exits
# ---------------------------------------------------------------------------

def monitor_positions(conn: sqlite3.Connection, broker=None, dry_run: bool = False) -> list[dict]:
    """
    Check open positions for exits that should trigger.
    Rules loaded from DB strategy parameters:
      - max_hold_days: close after N days
      - stop_loss_pct: close if drawdown exceeds threshold
    Returns list of positions closed.
    """
    closed = []
    now = datetime.now()

    # Load strategy params from DB
    pa_params = get_strategy_params(conn, PA_STRATEGY_ID) or _DEFAULT_PA_PARAMS
    trend_params = get_strategy_params(conn, TREND_STRATEGY_ID) or _DEFAULT_TREND_PARAMS

    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
    ).fetchall()

    # --- Regime-aware position management ---
    regime_data = get_current_regime(conn)
    current_regime = regime_data.get("regime") if regime_data else None
    if current_regime:
        log.info(f"  [REGIME] Current regime: {current_regime}")

    # CRISIS: close ALL trend positions immediately
    if current_regime == "CRISIS":
        trend_trades = [dict(r) for r in open_trades if dict(r)["strategy_id"] == TREND_STRATEGY_ID]
        if trend_trades:
            log.info(f"  [REGIME] CRISIS: closing ALL {len(trend_trades)} trend positions immediately")
            for t in trend_trades:
                exit_price = None
                pnl = None
                if broker:
                    try:
                        price_data = broker.get_price(t["symbol"])
                        if price_data:
                            exit_price = price_data["bid"]
                    except Exception:
                        pass
                if exit_price and t.get("entry_price"):
                    entry = float(t["entry_price"])
                    qty = float(t.get("quantity", 1))
                    pnl = ((exit_price - entry) if t["side"] == "long" else (entry - exit_price)) * qty

                if not dry_run:
                    conn.execute(
                        """UPDATE paper_trades
                           SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
                           WHERE id = ?""",
                        (now.strftime("%Y-%m-%dT%H:%M:%SZ"), exit_price, pnl, t["id"]),
                    )
                    conn.commit()
                    _close_broker_position(broker, t["symbol"], t.get("broker_order_id"))

                closed.append({**t, "close_reason": f"[REGIME] CRISIS: emergency close all trend positions"})
            # Reload open_trades after crisis closures
            open_trades = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
            ).fetchall()

    # RANGING: reduce trend positions to max 1, close weakest by trend strength
    if current_regime == "RANGING":
        trend_trades = [dict(r) for r in open_trades if dict(r)["strategy_id"] == TREND_STRATEGY_ID]
        if len(trend_trades) > RANGING_MAX_TREND_POSITIONS:
            excess = len(trend_trades) - RANGING_MAX_TREND_POSITIONS
            log.info(f"  [REGIME] RANGING: reducing trend positions from {len(trend_trades)} to "
                     f"{RANGING_MAX_TREND_POSITIONS}, closing weakest {excess}")

            # Rank by trend strength: unrealized P&L as proxy (weakest = lowest P&L)
            for t in trend_trades:
                t["_strength"] = 0.0
                if broker and t.get("entry_price"):
                    try:
                        price_data = broker.get_price(t["symbol"])
                        if price_data:
                            current = price_data["bid"]
                            entry = float(t["entry_price"])
                            t["_strength"] = (current - entry) if t["side"] == "long" else (entry - current)
                    except Exception:
                        pass

            # Sort ascending by strength — weakest first
            trend_trades.sort(key=lambda x: x["_strength"])
            to_close = trend_trades[:excess]

            for t in to_close:
                exit_price = None
                pnl = None
                if broker:
                    try:
                        price_data = broker.get_price(t["symbol"])
                        if price_data:
                            exit_price = price_data["bid"]
                    except Exception:
                        pass
                if exit_price and t.get("entry_price"):
                    entry = float(t["entry_price"])
                    qty = float(t.get("quantity", 1))
                    pnl = ((exit_price - entry) if t["side"] == "long" else (entry - exit_price)) * qty

                if not dry_run:
                    conn.execute(
                        """UPDATE paper_trades
                           SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
                           WHERE id = ?""",
                        (now.strftime("%Y-%m-%dT%H:%M:%SZ"), exit_price, pnl, t["id"]),
                    )
                    conn.commit()
                    _close_broker_position(broker, t["symbol"], t.get("broker_order_id"))

                closed.append({**t, "close_reason": f"[REGIME] RANGING: weakest trend position (strength={t['_strength']:+.5f})"})

            # Reload open_trades after ranging closures
            open_trades = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
            ).fetchall()

    # Determine ATR multiplier based on regime (VOLATILE = tighter)
    atr_multiplier = VOLATILE_ATR_MULTIPLIER if current_regime == "VOLATILE" else NORMAL_ATR_MULTIPLIER
    if current_regime == "VOLATILE":
        log.info(f"  [REGIME] VOLATILE: ATR multiplier tightened to {atr_multiplier}x (from {NORMAL_ATR_MULTIPLIER}x)")

    for row in open_trades:
        t = dict(row)
        should_close = False
        close_reason = ""

        # Get params for this strategy
        params = pa_params if t["strategy_id"] == PA_STRATEGY_ID else trend_params
        max_hold = params.get("max_hold_days")
        stop_pct = params.get("stop_loss_pct")
        is_trend = t["strategy_id"] == TREND_STRATEGY_ID

        # Safety net: trend positions without max_hold get a 90-day hard cap
        effective_max_hold = max_hold if max_hold else (TREND_SAFETY_MAX_HOLD if is_trend else None)

        # Check max hold period
        if effective_max_hold and t.get("opened_at"):
            opened = datetime.strptime(t["opened_at"][:19], "%Y-%m-%dT%H:%M:%S")
            days_held = (now - opened).days

            if days_held >= effective_max_hold:
                should_close = True
                label = "Safety max hold" if not max_hold else "Max hold"
                close_reason = f"{label} ({days_held} days >= {effective_max_hold})"

        # Bug fix: stop_pct check runs independently of max_hold
        if not should_close and stop_pct and broker and t.get("entry_price"):
            try:
                price_data = broker.get_price(t["symbol"])
                if price_data:
                    current = price_data["bid"]
                    entry = float(t["entry_price"])
                    pct_change = (current - entry) / entry
                    if pct_change <= -stop_pct:
                        should_close = True
                        close_reason = f"Stop loss ({pct_change:.1%} <= -{stop_pct:.0%})"
            except Exception:
                pass

        # ATR-adaptive trailing stop for TREND positions only
        if not should_close and is_trend and broker and t.get("entry_price"):
            try:
                price_data = broker.get_price(t["symbol"])
                if price_data:
                    current = price_data["bid"]
                    entry = float(t["entry_price"])
                    side = t.get("side", "long")
                    atr = get_atr(t["symbol"])

                    if atr and atr > 0:
                        if side == "long":
                            unrealized = current - entry
                            if unrealized >= atr_multiplier * atr:
                                # Profit trail: lock in at current - 1x ATR
                                effective_stop = current - atr
                                if current <= effective_stop:
                                    should_close = True
                                    close_reason = (f"ATR trailing stop (unrealized={unrealized:+.5f}, "
                                                    f"ATR={atr:.5f}, trail @ {effective_stop:.5f})")
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, trailing stop @ {effective_stop:.5f}")
                            elif unrealized >= 1 * atr:
                                # Break-even lock
                                effective_stop = entry
                                if current <= effective_stop:
                                    should_close = True
                                    close_reason = f"ATR break-even stop (ATR={atr:.5f})"
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, stop=break-even @ {entry:.5f}")
                            else:
                                # Initial stop: Nx ATR below entry (regime-aware)
                                effective_stop = entry - atr_multiplier * atr
                                if current <= effective_stop:
                                    should_close = True
                                    close_reason = (f"ATR initial stop (current={current:.5f} <= "
                                                    f"stop={effective_stop:.5f}, {atr_multiplier}xATR={atr_multiplier*atr:.5f})")
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, initial stop @ {effective_stop:.5f}")
                        else:
                            # Short position: mirror logic
                            unrealized = entry - current
                            if unrealized >= atr_multiplier * atr:
                                effective_stop = current + atr
                                if current >= effective_stop:
                                    should_close = True
                                    close_reason = (f"ATR trailing stop (unrealized={unrealized:+.5f}, "
                                                    f"ATR={atr:.5f}, trail @ {effective_stop:.5f})")
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, trailing stop @ {effective_stop:.5f}")
                            elif unrealized >= 1 * atr:
                                effective_stop = entry
                                if current >= effective_stop:
                                    should_close = True
                                    close_reason = f"ATR break-even stop (ATR={atr:.5f})"
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, stop=break-even @ {entry:.5f}")
                            else:
                                effective_stop = entry + atr_multiplier * atr
                                if current >= effective_stop:
                                    should_close = True
                                    close_reason = (f"ATR initial stop (current={current:.5f} >= "
                                                    f"stop={effective_stop:.5f}, {atr_multiplier}xATR={atr_multiplier*atr:.5f})")
                                else:
                                    log.info(f"  [TRAIL] {t['symbol']}: ATR={atr:.5f}, "
                                             f"unrealized={unrealized:+.5f}, initial stop @ {effective_stop:.5f}")
                    else:
                        # ATR unavailable — fall back to fixed 80-pip stop
                        pip_mult = 0.01 if "JPY" in t["symbol"] else 0.0001
                        fixed_stop_dist = 80 * pip_mult
                        if side == "long" and current <= entry - fixed_stop_dist:
                            should_close = True
                            close_reason = f"Fixed 80-pip stop (no ATR, current={current:.5f})"
                        elif side == "short" and current >= entry + fixed_stop_dist:
                            should_close = True
                            close_reason = f"Fixed 80-pip stop (no ATR, current={current:.5f})"
            except Exception as e:
                log.warning(f"  [TRAIL] {t['symbol']}: error checking ATR stop: {e}")

        if should_close:
            log.info(f"  MONITOR EXIT: {t['symbol']} — {close_reason}")
            if not dry_run:
                # Get current price for P&L
                exit_price = None
                if broker:
                    try:
                        price_data = broker.get_price(t["symbol"])
                        if price_data:
                            exit_price = price_data["bid"]
                    except Exception:
                        pass

                # Calculate P&L
                pnl = None
                if exit_price and t.get("entry_price"):
                    entry = float(t["entry_price"])
                    qty = float(t.get("quantity", 1))
                    if t["side"] == "long":
                        pnl = (exit_price - entry) * qty
                    else:
                        pnl = (entry - exit_price) * qty

                conn.execute(
                    """UPDATE paper_trades
                       SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
                       WHERE id = ?""",
                    (now.strftime("%Y-%m-%dT%H:%M:%SZ"), exit_price, pnl, t["id"]),
                )
                conn.commit()

                _close_broker_position(broker, t["symbol"], t.get("broker_order_id"))

            closed.append({**t, "close_reason": close_reason})

    return closed


# ---------------------------------------------------------------------------
# Risk check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Correlation guard — prevent concentrated currency exposure
# ---------------------------------------------------------------------------

# Currency decomposition: long = +base -quote
PAIR_CURRENCIES = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"), "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"),
    "NZDUSD": ("NZD", "USD"), "EURGBP": ("EUR", "GBP"), "EURJPY": ("EUR", "JPY"),
    "GBPJPY": ("GBP", "JPY"),
}

MAX_CURRENCY_EXPOSURE = 3  # block at ±3
WARN_CURRENCY_EXPOSURE = 2  # reduce size at ±2


def get_currency_exposure(open_positions: list[dict]) -> dict[str, int]:
    """Count each currency's net directional exposure from open positions.

    Long AUDUSD = +1 AUD, -1 USD.  Short USDJPY = -1 USD, +1 JPY.
    Returns e.g. {"AUD": 1, "USD": -2, "JPY": -1, ...}
    """
    exposure: dict[str, int] = {}
    for pos in open_positions:
        symbol = pos["symbol"]
        side = pos.get("side", "long")
        currencies = PAIR_CURRENCIES.get(symbol)
        if not currencies:
            continue
        base, quote = currencies
        direction = 1 if side == "long" else -1
        exposure[base] = exposure.get(base, 0) + direction
        exposure[quote] = exposure.get(quote, 0) - direction
    return exposure


def check_correlation_guard(
    new_pair: str,
    new_side: str,
    open_positions: list[dict],
) -> tuple[bool, str, float]:
    """Check if adding *new_pair* would breach currency-exposure limits.

    Returns (allowed, reason, size_multiplier).
      - allowed=False  → trade blocked
      - size_multiplier=0.5 → reduce size by half
      - size_multiplier=1.0 → full size OK
    """
    currencies = PAIR_CURRENCIES.get(new_pair)
    if not currencies:
        return True, "", 1.0

    current = get_currency_exposure(open_positions)
    base, quote = currencies
    direction = 1 if new_side == "long" else -1

    new_base_exp = current.get(base, 0) + direction
    new_quote_exp = current.get(quote, 0) - direction

    # Check block threshold (±3)
    for ccy, exp in [(base, new_base_exp), (quote, new_quote_exp)]:
        if abs(exp) >= MAX_CURRENCY_EXPOSURE:
            reason = (f"correlation guard BLOCK: {ccy} exposure would be {exp:+d} "
                      f"(limit ±{MAX_CURRENCY_EXPOSURE})")
            log.warning(reason)
            return False, reason, 0.0

    # Check reduce threshold (±2)
    for ccy, exp in [(base, new_base_exp), (quote, new_quote_exp)]:
        if abs(exp) >= WARN_CURRENCY_EXPOSURE:
            reason = (f"correlation guard REDUCE: {ccy} exposure would be {exp:+d} "
                      f"(warn ±{WARN_CURRENCY_EXPOSURE}) — size halved")
            log.info(reason)
            return True, reason, 0.5

    return True, "", 1.0


def fx_risk_check(conn: sqlite3.Connection, signals: list[dict]) -> list[dict]:
    """
    FX risk manager:
    - Max 3 positions total (regime-adjusted)
    - 2% risk per trade
    - Strategy-specific stop loss pips
    - Correlation guard: block/reduce concentrated currency exposure
    - Regime compliance: RANGING limits trend entries, CRISIS blocks all
    """
    # Load regime for entry gating
    regime_data = get_current_regime(conn)
    current_regime = regime_data.get("regime") if regime_data else None

    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at"
    ).fetchall()
    open_count = len(open_trades)
    open_symbols = [dict(t)["symbol"] for t in open_trades]

    # Regime-adjusted max positions for trend
    open_trend_count = sum(1 for t in open_trades if dict(t)["strategy_id"] == TREND_STRATEGY_ID)
    # Track open symbol+strategy pairs for dedup (prevents duplicate entries same day)
    open_sym_strat = {(dict(t)["symbol"], dict(t)["strategy_id"]) for t in open_trades}
    # Track open positions as dicts for correlation guard (updated as we approve entries)
    open_pos_list = [dict(t) for t in open_trades]

    # Log current exposure at start
    current_exp = get_currency_exposure(open_pos_list)
    if current_exp:
        exp_str = ", ".join(f"{c}:{v:+d}" for c, v in sorted(current_exp.items()) if v != 0)
        log.info(f"  Currency exposure: {exp_str}")

    decisions = []

    for signal in signals:
        if signal["signal_type"] == "exit":
            decisions.append({**signal, "approved": True, "action": "exit"})
            continue

        symbol = signal["symbol"]

        if open_count >= MAX_POSITIONS:
            decisions.append({**signal, "approved": False, "reason": f"max {MAX_POSITIONS} positions"})
            continue

        strategy_id = signal.get("strategy_id")
        if (symbol, strategy_id) in open_sym_strat:
            decisions.append({**signal, "approved": False, "reason": f"already holding {symbol} (strategy {strategy_id})"})
            continue

        # Regime gating for new trend entries
        if strategy_id == TREND_STRATEGY_ID and current_regime:
            if current_regime == "CRISIS":
                decisions.append({**signal, "approved": False,
                                  "reason": f"[REGIME] CRISIS: no new trend entries"})
                continue
            if current_regime == "RANGING":
                if open_trend_count >= RANGING_MAX_TREND_POSITIONS:
                    decisions.append({**signal, "approved": False,
                                      "reason": f"[REGIME] RANGING: trend positions capped at {RANGING_MAX_TREND_POSITIONS}"})
                    continue
                # Block weak trend signals in ranging — require high composite score
                full_state = signal.get("full_state") or {}
                composite = signal.get("composite_score") or full_state.get("composite_score", 0)
                if composite < RANGING_MIN_COMPOSITE_SCORE:
                    decisions.append({**signal, "approved": False,
                                      "reason": f"[REGIME] RANGING: trend composite score {composite} < {RANGING_MIN_COMPOSITE_SCORE} threshold"})
                    continue

        # Correlation guard
        allowed, corr_reason, size_mult = check_correlation_guard(
            symbol, signal["side"], open_pos_list
        )
        if not allowed:
            decisions.append({**signal, "approved": False, "reason": corr_reason})
            continue

        # Strategy-specific stop loss from DB
        params = get_strategy_params(conn, signal["strategy_id"])
        default = _DEFAULT_TREND_PARAMS if signal["strategy_id"] == TREND_STRATEGY_ID else _DEFAULT_PA_PARAMS
        stop_pips = params.get("stop_loss_pips", default["stop_loss_pips"])

        # Position sizing: risk amount / (stop_pips * pip_value)
        pip_value = 0.10  # approx for micro lot
        risk_amount = ACCOUNT_BALANCE * MAX_RISK_PER_TRADE
        micro_lots = risk_amount / (stop_pips * pip_value)
        volume = max(int(micro_lots), 1)

        # Apply correlation size reduction
        if size_mult < 1.0:
            volume = max(int(volume * size_mult), 1)
            risk_amount = round(risk_amount * size_mult, 2)

        decisions.append({
            **signal,
            "approved": True,
            "action": "entry",
            "micro_lots": volume,
            "risk_amount": round(risk_amount, 2),
            "stop_pips": stop_pips,
            "risk_pct": MAX_RISK_PER_TRADE * 100,
            **({"corr_note": corr_reason} if corr_reason else {}),
        })
        open_count += 1
        if strategy_id == TREND_STRATEGY_ID:
            open_trend_count += 1
        open_symbols.append(symbol)
        open_sym_strat.add((symbol, strategy_id))
        open_pos_list.append({"symbol": symbol, "side": signal["side"]})

    return decisions


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def execute_decisions(conn: sqlite3.Connection, decisions: list[dict], dry_run: bool = False):
    """Execute approved decisions on DB and broker."""
    broker = _get_broker() if not dry_run else None

    for d in decisions:
        if not d["approved"]:
            log.info(f"  VETOED: {d['symbol']} — {d.get('reason', 'unknown')}")
            continue

        if d["action"] == "entry":
            log.info(f"  ENTRY: {d['symbol']} {d['micro_lots']} micro lots, "
                     f"risk=${d['risk_amount']} ({d['risk_pct']}%), stop={d['stop_pips']}pips "
                     f"[{'TREND' if d['strategy_id'] == TREND_STRATEGY_ID else 'PA'}]")

            if not dry_run:
                if not broker:
                    log.warning(f"  SKIPPED {d['symbol']}: no broker connected — refusing to create ghost trade")
                    continue

                # Submit to broker FIRST — only write to DB if broker confirms
                broker_order_id = None
                try:
                    units = d["micro_lots"] * 1000
                    result = broker.submit_order(
                        symbol=d["symbol"],
                        units=units,
                        side=d["side"],
                        stop_loss_pips=d["stop_pips"],
                        take_profit_pips=None,
                    )
                    broker_order_id = result.get("deal_id") or result.get("trade_id")
                    log.info(f"    Broker order: {result}")

                    if not broker_order_id:
                        log.error(f"  SKIPPED {d['symbol']}: broker returned no deal_id — not writing to DB")
                        continue

                except Exception as e:
                    log.error(f"  SKIPPED {d['symbol']}: broker execution failed: {e} — not writing to DB")
                    continue

                # Broker confirmed — now write to DB with order ID
                conn.execute(
                    """INSERT INTO paper_trades
                       (strategy_id, signal_id, symbol, side, entry_price,
                        quantity, thesis, risk_pct, risk_approved, status, broker_order_id, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?, ?)""",
                    (
                        d["strategy_id"], d.get("signal_id"),
                        d["symbol"], d["side"], d["price_at_signal"],
                        d["micro_lots"],
                        f"FX {d['strategy']}: {d['symbol']} @ {d['price_at_signal']}",
                        d["risk_pct"],
                        broker_order_id,
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                conn.commit()
                log.info(f"    DB trade created with broker_order_id={broker_order_id}")

        elif d["action"] == "exit":
            log.info(f"  EXIT: {d['symbol']}")
            if not dry_run:
                # Get broker order ID before closing
                trade_row = conn.execute(
                    "SELECT id, broker_order_id, entry_price, quantity, side FROM paper_trades WHERE symbol = ? AND status = 'open' LIMIT 1",
                    (d["symbol"],),
                ).fetchone()

                if trade_row:
                    trade = dict(trade_row)

                    # Calculate P&L
                    pnl = None
                    exit_price = d.get("price_at_signal")
                    if exit_price and trade.get("entry_price"):
                        entry = float(trade["entry_price"])
                        qty = float(trade.get("quantity", 1))
                        if trade["side"] == "long":
                            pnl = (float(exit_price) - entry) * qty
                        else:
                            pnl = (entry - float(exit_price)) * qty

                    conn.execute(
                        """UPDATE paper_trades
                           SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
                           WHERE id = ?""",
                        (datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), exit_price, pnl, trade["id"]),
                    )
                    conn.commit()

                    # Close on broker
                    if broker:
                        _close_broker_position(broker, d["symbol"], trade.get("broker_order_id"))

    if broker:
        broker.disconnect()


# ---------------------------------------------------------------------------
# Telegram + Status
# ---------------------------------------------------------------------------

def _send_fx_telegram(conn: sqlite3.Connection, signals: list[dict], mode: str,
                      closed_by_monitor: list[dict] = None, regime: dict = None,
                      perf_alerts: list[dict] = None):
    """Send FX pipeline summary to Telegram."""
    import requests as _req

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
    ).fetchall()
    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
    ).fetchall()

    realized = sum(dict(t).get("pnl", 0) or 0 for t in closed_trades)
    wins = sum(1 for t in closed_trades if (dict(t).get("pnl", 0) or 0) > 0)
    total = len(closed_trades)
    win_rate = (wins / total * 100) if total > 0 else 0

    entries = [s for s in signals if s.get("signal_type") == "entry"]
    exits = [s for s in signals if s.get("signal_type") == "exit"]

    lines = [
        f"<b>FX Pipeline — {datetime.now().strftime('%Y-%m-%d')}</b>",
        f"Mode: {mode}",
    ]

    # Regime info
    if regime:
        r = regime["regime"]
        vix = regime.get("vix")
        adx = regime.get("avg_adx")
        vix_str = f"VIX:{vix}" if vix else ""
        adx_str = f"ADX:{adx}" if adx else ""
        lines.append(f"Regime: <b>{r}</b> ({vix_str} {adx_str})")
    lines.append("")

    if entries:
        lines.append(f"<b>ENTRIES ({len(entries)})</b>")
        for s in entries:
            tag = "TREND" if s.get("strategy_id") == TREND_STRATEGY_ID else "PA"
            lines.append(f"  [{tag}] {s['symbol']} {s['side'].upper()} @ {s['price_at_signal']}")
        lines.append("")

    if exits:
        lines.append(f"<b>EXITS ({len(exits)})</b>")
        for s in exits:
            lines.append(f"  {s['symbol']} @ {s['price_at_signal']}")
        lines.append("")

    if closed_by_monitor:
        lines.append(f"<b>MONITOR EXITS ({len(closed_by_monitor)})</b>")
        for c in closed_by_monitor:
            lines.append(f"  {c['symbol']} — {c['close_reason']}")
        lines.append("")

    if not signals and not closed_by_monitor:
        lines.append("No signals today.")
        lines.append("")

    lines += [
        f"<b>PORTFOLIO</b>",
        f"  Account: ${ACCOUNT_BALANCE:.0f}",
        f"  Open: {len(open_trades)} position(s)",
        f"  Realized: ${realized:+.2f}",
        f"  Win Rate: {win_rate:.0f}% ({wins}/{total})",
    ]

    if open_trades:
        lines.append("")
        for t in open_trades:
            t = dict(t)
            tag = "T" if t["strategy_id"] == TREND_STRATEGY_ID else "PA"
            opened = t.get("opened_at", "?")[:10]
            lines.append(f"  [{tag}] {t['symbol']} {t['side']} {t.get('quantity', '?')} lots @ {t['entry_price']} ({opened})")

    # Performance alerts
    if perf_alerts:
        lines.append("")
        lines.append(f"<b>ALERTS ({len(perf_alerts)})</b>")
        for a in perf_alerts:
            lines.append(f"  [{a['severity']}] {a['message']}")

    msg_text = "\n".join(lines)
    # Telegram has a 4096 char limit
    if len(msg_text) > 4000:
        msg_text = msg_text[:4000] + "\n..."
    try:
        _req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        # Retry without HTML parse_mode (malformed tags cause 400)
        log.warning(f"Telegram HTML failed: {e}, retrying as plain text")
        try:
            _req.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg_text.replace("<b>", "").replace("</b>", "")},
                timeout=10,
            ).raise_for_status()
        except Exception as e2:
            log.error(f"Telegram alert failed: {e2}")


def fx_portfolio_status(conn: sqlite3.Connection):
    """Show FX portfolio status."""
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
    ).fetchall()
    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
    ).fetchall()

    realized = sum(dict(t).get("pnl", 0) or 0 for t in closed_trades)
    wins = sum(1 for t in closed_trades if (dict(t).get("pnl", 0) or 0) > 0)
    total = len(closed_trades)
    win_rate = (wins / total * 100) if total > 0 else 0

    print(f"\n{'='*60}")
    print(f"FX PORTFOLIO — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    print(f"  Account:    ${ACCOUNT_BALANCE:.0f} (leverage {LEVERAGE:.0f}:1)")
    print(f"  Realized:   ${realized:+.2f}")
    print(f"  Win Rate:   {win_rate:.0f}% ({wins}/{total})")
    print(f"  Open:       {len(open_trades)} position(s)")

    if open_trades:
        print(f"\n  OPEN POSITIONS:")
        for t in open_trades:
            t = dict(t)
            tag = "TREND" if t["strategy_id"] == TREND_STRATEGY_ID else "PA"
            opened = t.get("opened_at", "?")[:10]
            days = ""
            if t.get("opened_at"):
                try:
                    d = (datetime.now() - datetime.strptime(t["opened_at"][:19], "%Y-%m-%dT%H:%M:%S")).days
                    days = f" ({d}d)"
                except Exception:
                    pass
            print(f"    [{tag}] {t['symbol']:>8} {t['side']} {t.get('quantity', '?')} lots "
                  f"@ {t['entry_price']} — {opened}{days}")


# ---------------------------------------------------------------------------
# Daily runner
# ---------------------------------------------------------------------------

def run_daily(dry_run: bool = False, db_path: str | None = None):
    conn = init_db(db_path)

    has_capital = bool(os.environ.get("CAPITAL_API_KEY"))
    has_oanda = bool(os.environ.get("OANDA_API_KEY"))
    has_ctrader = bool(os.environ.get("CTRADER_CLIENT_ID"))
    mode = "CAPITAL.COM" if has_capital else "OANDA" if has_oanda else "cTrader" if has_ctrader else "SQLITE-ONLY"

    print(f"\n{'#'*60}")
    print(f"# FX PIPELINE — {datetime.now().strftime('%Y-%m-%d %H:%M')} [{mode}]")
    print(f"{'#'*60}")

    # Pre-flight: verify broker credentials and sync positions
    if not dry_run:
        print("\n[PRE] Broker credential check...")
        try:
            broker_test = _get_broker()
            if broker_test:
                acct = broker_test.get_account()
                print(f"  CONNECTED: {mode} | balance=${acct.get('balance', '?')} | equity=${acct.get('equity', '?')}")

                # Sync: detect broker positions missing from DB
                broker_positions = broker_test.get_positions()
                db_open = conn.execute(
                    "SELECT symbol, broker_order_id FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
                ).fetchall()
                db_deal_ids = {dict(r).get("broker_order_id") for r in db_open}
                db_symbols = {dict(r)["symbol"] for r in db_open}

                for bp in broker_positions:
                    epic = bp["epic"].upper().replace("/", "")
                    if bp["deal_id"] not in db_deal_ids and epic not in db_symbols:
                        print(f"  SYNC: Found broker position {epic} (deal={bp['deal_id']}) not in DB — importing")
                        side = "long" if bp["direction"] == "BUY" else "short"
                        conn.execute(
                            """INSERT INTO paper_trades
                               (strategy_id, symbol, side, entry_price, quantity, thesis,
                                risk_pct, risk_approved, status, broker_order_id, opened_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'open', ?, ?)""",
                            (TREND_STRATEGY_ID, epic, side, bp["entry_price"], bp["size"],
                             f"Synced from broker: {epic} {side} @ {bp['entry_price']}",
                             MAX_RISK_PER_TRADE * 100, bp["deal_id"], bp.get("created_date", datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))),
                        )
                        conn.commit()
                        print(f"    Imported: {epic} {side} @ {bp['entry_price']}, deal={bp['deal_id']}")

                if not broker_positions:
                    print(f"  No open positions on broker")
                broker_test.disconnect()
            else:
                print(f"  WARNING: No broker credentials found — running in SQLITE-ONLY mode (no real trades)")
        except Exception as e:
            print(f"  ERROR: Broker connection failed: {e}")
            print(f"  Continuing in SQLITE-ONLY mode — signals will be generated but NOT executed")
            mode = "SQLITE-ONLY (broker auth failed)"

    # Step 0: Monitor existing positions (time stops, % stops)
    print("\n[0/6] Monitoring open positions...")
    broker_for_monitor = _get_broker() if not dry_run else None
    closed_by_monitor = monitor_positions(conn, broker=broker_for_monitor, dry_run=dry_run)
    if closed_by_monitor:
        print(f"  Closed {len(closed_by_monitor)} position(s) by monitor")
    else:
        print("  All positions OK")
    if broker_for_monitor:
        broker_for_monitor.disconnect()

    # Step 1: Regime detection — determine market conditions before trading
    print("\n[1/6] Regime detection...")
    regime_result = None
    paused_strategies = set()
    reduced_strategies = set()
    try:
        from pipeline.agents.regime_detector import classify_regime, get_vix, get_fx_adx
        vix = get_vix()
        fx_adx = get_fx_adx()
        regime_result = classify_regime(vix, fx_adx)
        regime = regime_result["regime"]
        print(f"  Regime: {regime} — {regime_result['description']}")

        for sid, rec in regime_result.get("recommendations", {}).items():
            action = rec["action"]
            name = {100: "FX Trend", 101: "FX Price Action"}.get(sid, f"Strategy {sid}")
            print(f"    [{sid}] {name}: {action} — {rec['reason']}")
            if action == "PAUSE":
                paused_strategies.add(sid)
            elif action == "REDUCE":
                reduced_strategies.add(sid)

        log_agent_action(conn, "regime_detector", "regime_classified", outputs=regime_result)
    except Exception as e:
        log.warning(f"Regime detection failed (continuing): {e}")
        print(f"  Regime detection failed: {e} — proceeding with defaults")

    # Write regime data to shared/global_state.json for other bots
    state_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "global_state.json")
    try:
        existing = {}
        if os.path.exists(state_path):
            with open(state_path) as f:
                existing = json.load(f)

        regime_state = {
            "regime": regime_result["regime"] if regime_result else None,
            "vix": vix if regime_result else None,
            "recommendations": regime_result.get("recommendations", {}) if regime_result else {},
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        existing.update(regime_state)

        with open(state_path, "w") as f:
            json.dump(existing, f, indent=2)
        log.info(f"Regime data written to {state_path}")
    except Exception as e:
        log.warning(f"Failed to write regime to global_state.json: {e}")

    # Step 2: Generate signals
    from pipeline.agents.fx_signal_generator import generate_fx_signals
    print("\n[2/6] Generating FX signals...")
    signals = generate_fx_signals(dry_run=dry_run, db_path=db_path)

    # Filter signals based on regime
    if paused_strategies or reduced_strategies:
        filtered = []
        for s in signals:
            sid = s.get("strategy_id")
            if s["signal_type"] == "exit":
                # Always allow exits
                filtered.append(s)
            elif sid in paused_strategies:
                print(f"  REGIME PAUSE: skipping {s['symbol']} entry (strategy {sid})")
            else:
                filtered.append(s)
        signals = filtered

    # Step 3: Risk check
    print("\n[3/6] Risk management...")
    if signals:
        decisions = fx_risk_check(conn, signals)

        # Halve position sizes for REDUCE strategies
        for d in decisions:
            if d.get("approved") and d.get("action") == "entry" and d.get("strategy_id") in reduced_strategies:
                d["micro_lots"] = max(d["micro_lots"] // 2, 1)
                d["risk_amount"] = round(d["risk_amount"] / 2, 2)
                print(f"  REGIME REDUCE: {d['symbol']} position halved to {d['micro_lots']} micro lots")

        approved = [d for d in decisions if d["approved"]]
        vetoed = [d for d in decisions if not d["approved"]]
        print(f"  {len(approved)} approved, {len(vetoed)} vetoed")

        # Step 4: Execute
        print("\n[4/6] Executing...")
        execute_decisions(conn, decisions, dry_run=dry_run)
    else:
        print("  No signals to evaluate.")
        print("\n[4/6] Nothing to execute.")

    # Step 5: Performance monitor
    print("\n[5/6] Performance check...")
    perf_results = []
    perf_alerts = []
    try:
        from pipeline.agents.performance_monitor import analyze_strategy
        for sid in [TREND_STRATEGY_ID, PA_STRATEGY_ID]:
            r = analyze_strategy(conn, sid)
            perf_results.append(r)
            if r["status"] == "no_data":
                print(f"  [{sid}] {r['name']}: no closed trades yet")
            else:
                sharpe_str = f"{r['rolling_sharpe']:.2f}" if r['rolling_sharpe'] is not None else "N/A"
                print(f"  [{sid}] {r['name']}: Sharpe={sharpe_str} WR={r['win_rate']}% P&L=${r['total_pnl']:+.2f}")
                for a in r["alerts"]:
                    perf_alerts.append(a)
                    print(f"    [{a['severity']}] {a['message']}")

        log_agent_action(conn, "performance_monitor", "daily_check",
                         outputs={"strategies": len(perf_results), "alerts": len(perf_alerts)})
    except Exception as e:
        log.warning(f"Performance monitor failed (continuing): {e}")
        print(f"  Performance monitor failed: {e}")

    # Step 6: Status + Telegram
    print("\n[6/6] Portfolio status...")
    fx_portfolio_status(conn)

    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        try:
            _send_fx_telegram(conn, signals, mode, closed_by_monitor,
                              regime=regime_result, perf_alerts=perf_alerts)
            print("  Telegram alert sent.")
        except Exception as e:
            log.error(f"Telegram alert failed: {e}")

    log_agent_action(
        conn, "fx_pipeline", "daily_completed",
        outputs={
            "signals": len(signals),
            "mode": mode,
            "dry_run": dry_run,
            "monitor_closed": len(closed_by_monitor),
            "regime": regime_result["regime"] if regime_result else None,
            "paused": list(paused_strategies),
            "reduced": list(reduced_strategies),
            "perf_alerts": len(perf_alerts),
        },
    )


def main():
    parser = argparse.ArgumentParser(description="FX Pipeline — daily forex trading")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    if args.daily:
        run_daily(dry_run=args.dry_run, db_path=args.db)
    elif args.status:
        conn = init_db(args.db)
        fx_portfolio_status(conn)
    else:
        conn = init_db(args.db)
        fx_portfolio_status(conn)


if __name__ == "__main__":
    main()
