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

    for row in open_trades:
        t = dict(row)
        should_close = False
        close_reason = ""

        # Get params for this strategy
        params = pa_params if t["strategy_id"] == PA_STRATEGY_ID else trend_params
        max_hold = params.get("max_hold_days")
        stop_pct = params.get("stop_loss_pct")

        # Check max hold period
        if max_hold and t.get("opened_at"):
            opened = datetime.strptime(t["opened_at"][:19], "%Y-%m-%dT%H:%M:%S")
            days_held = (now - opened).days

            if days_held >= max_hold:
                should_close = True
                close_reason = f"Max hold ({days_held} days >= {max_hold})"

            # Check % stop loss if broker has live price
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
                        pnl = (exit_price - entry) * qty * 1000  # micro lots * 1000
                    else:
                        pnl = (entry - exit_price) * qty * 1000

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
    - Max 3 positions total
    - 2% risk per trade
    - Strategy-specific stop loss pips
    - Correlation guard: block/reduce concentrated currency exposure
    """
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at"
    ).fetchall()
    open_count = len(open_trades)
    open_symbols = [dict(t)["symbol"] for t in open_trades]
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

        if symbol in open_symbols:
            decisions.append({**signal, "approved": False, "reason": f"already holding {symbol}"})
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
        open_symbols.append(symbol)
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
                conn.execute(
                    """INSERT INTO paper_trades
                       (strategy_id, signal_id, symbol, side, entry_price,
                        quantity, thesis, risk_pct, risk_approved, status, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?)""",
                    (
                        d["strategy_id"], d.get("signal_id"),
                        d["symbol"], d["side"], d["price_at_signal"],
                        d["micro_lots"],
                        f"FX {d['strategy']}: {d['symbol']} @ {d['price_at_signal']}",
                        d["risk_pct"],
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                conn.commit()

                if broker:
                    try:
                        units = d["micro_lots"] * 1000
                        # Trend: no TP (hold until SMA exit), wider SL
                        # PA: no TP on broker (managed by monitor), tighter SL
                        result = broker.submit_order(
                            symbol=d["symbol"],
                            units=units,
                            side=d["side"],
                            stop_loss_pips=d["stop_pips"],
                            take_profit_pips=None,  # managed by signals/monitor, not fixed TP
                        )
                        # Store broker order ID
                        order_id = result.get("deal_id") or result.get("trade_id")
                        if order_id:
                            conn.execute(
                                "UPDATE paper_trades SET broker_order_id = ? WHERE symbol = ? AND status = 'open' AND broker_order_id IS NULL",
                                (order_id, d["symbol"]),
                            )
                            conn.commit()
                        log.info(f"    Broker order: {result}")
                    except Exception as e:
                        log.error(f"    Broker execution failed: {e}")

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
                            pnl = (float(exit_price) - entry) * qty * 1000
                        else:
                            pnl = (entry - float(exit_price)) * qty * 1000

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

    _req.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"},
        timeout=10,
    ).raise_for_status()


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
