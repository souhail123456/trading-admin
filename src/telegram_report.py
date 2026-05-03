"""
Trading Admin — Unified Telegram Reports
-----------------------------------------
Sends consolidated reports across all 3 bots to Telegram.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

SHANGHAI_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

def send_unified_report(
    stock_stats: dict | None,
    fx_stats: dict | None,
    poly_stats: dict | None,
) -> None:
    """Send the combined daily report covering all 3 bots."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    msg = _build_unified_report(stock_stats, fx_stats, poly_stats)
    _send_telegram(bot_token, chat_id, msg)


def send_fx_report(fx_stats: dict) -> None:
    """Send FX-only report."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    msg = _build_fx_section(fx_stats)
    _send_telegram(bot_token, chat_id, msg)


def send_daily_report(stock_stats: dict, poly_stats: dict) -> None:
    """Legacy: send stock + poly report (backward compat)."""
    send_unified_report(stock_stats, None, poly_stats)


def send_signal_alert(signals: list[dict], portfolio: dict | None = None) -> None:
    """Send trading signal alerts."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    msg = _build_signal_report(signals, portfolio)
    _send_telegram(bot_token, chat_id, msg)


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

def _build_unified_report(
    stock: dict | None,
    fx: dict | None,
    poly: dict | None,
) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_sh = datetime.now(SHANGHAI_TZ).strftime("%H:%M Shanghai")
    now = f"{now_utc} ({now_sh})"

    # Totals
    total_pnl = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    bots_online = 0

    for s in [stock, fx, poly]:
        if s:
            bots_online += 1
            total_pnl += s.get("total_pnl", 0)
            total_trades += s.get("total_trades", 0)
            total_wins += s.get("win_count", 0)
            total_losses += s.get("loss_count", 0)

    win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0

    lines = [
        f"<b>TRADING ADMIN — Daily Report</b>",
        f"{now}",
        "",
        f"<b>OVERVIEW ({bots_online}/3 bots online)</b>",
        f"  Total P&L: <b>${total_pnl:+,.2f}</b>",
        f"  Trades: {total_trades} | Win rate: {win_rate:.0f}% ({total_wins}W/{total_losses}L)",
        "",
    ]

    # Stock Bot
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    if stock:
        lines += _build_stock_lines(stock)
    else:
        lines += ["<b>STOCK BOT</b> — offline"]
    lines.append("")

    # FX Bot
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    if fx:
        lines += _build_fx_lines(fx)
    else:
        lines += ["<b>FX BOT</b> — offline"]
    lines.append("")

    # Polymarket Bot
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    if poly:
        lines += _build_poly_lines(poly)
    else:
        lines += ["<b>POLYMARKET BOT</b> — offline"]

    return "\n".join(lines)


def _build_stock_lines(s: dict) -> list[str]:
    portfolio = s.get("portfolio_value")
    cash = s.get("cash")
    closed = s.get("trades", [])
    closed_count = len(closed)
    win_rate = (s["win_count"] / closed_count * 100) if closed_count > 0 else 0

    lines = [
        f"<b>STOCK BOT</b> (Alpaca)",
    ]

    if portfolio is not None:
        lines.append(f"  Portfolio: ${portfolio:,.2f} (P&L: ${s['total_pnl']:+,.2f})")
    else:
        lines.append(f"  P&L: ${s['total_pnl']:+,.2f}")

    if cash is not None:
        invested = portfolio - cash if portfolio else 0
        lines.append(f"  Cash: ${cash:,.2f} | Invested: ${invested:,.2f}")

    open_pos = s.get("open_positions", [])
    if open_pos:
        lines.append(f"  Open positions: {len(open_pos)}")
        for p in open_pos:
            unrealized = p.get("unrealized_pnl", 0)
            lines.append(f"    {p['symbol']} {p['side']} {p['shares']}@${p['entry']} "
                         f"(${unrealized:+,.2f})")

    if closed_count > 0:
        lines.append(f"  Closed: {closed_count} | W/L: {s['win_count']}W/{s['loss_count']}L ({win_rate:.0f}%)")

    lines.append(f"  Last run: {s.get('last_run') or 'N/A'}")
    return lines


def _build_fx_lines(s: dict) -> list[str]:
    win_rate = (s["win_count"] / (s["win_count"] + s["loss_count"]) * 100) if (s["win_count"] + s["loss_count"]) > 0 else 0
    lines = [
        f"<b>FX BOT</b> ({s.get('broker', 'Capital.com')})",
        f"  P&L: ${s['total_pnl']:+,.2f} (realized)",
        f"  Open: {s['open_positions']} position(s)",
        f"  Closed: {s['total_trades'] - s['open_positions']} | Win rate: {win_rate:.0f}%",
        f"  Last run: {s.get('last_run') or 'N/A'}",
    ]

    if s.get("open_trades"):
        lines.append("  Positions:")
        for t in s["open_trades"]:
            symbol = t.get("symbol", "?")
            side = t.get("side", "?")
            qty = t.get("quantity", "?")
            entry = t.get("entry_price", "?")
            lines.append(f"    {symbol} {side} {qty} lots @ {entry}")

    return lines


def _build_fx_section(s: dict) -> str:
    """Standalone FX report."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_sh = datetime.now(SHANGHAI_TZ).strftime("%H:%M Shanghai")
    now = f"{now_utc} ({now_sh})"
    lines = [
        f"<b>FX BOT — Report</b>",
        f"{now}",
        "",
    ]
    lines += _build_fx_lines(s)
    return "\n".join(lines)


def _build_poly_lines(s: dict) -> list[str]:
    total_resolved = s.get("win_count", 0) + s.get("loss_count", 0)
    win_rate = (s["win_count"] / total_resolved * 100) if total_resolved > 0 else 0
    lines = [
        f"<b>POLYMARKET BOT</b>",
        f"  P&L: ${s['total_pnl']:+,.2f} (resolved)",
        f"  Trades: {s['total_trades']} ({s.get('open_positions', 0)} open)",
        f"  W/L: {s['win_count']}W / {s['loss_count']}L ({win_rate:.0f}%)",
        f"  Last run: {s.get('last_run') or 'N/A'}",
    ]

    # EV bot breakdown
    ev_total = s.get("ev_total", 0)
    if ev_total:
        ev_resolved = s.get("ev_wins", 0) + s.get("ev_losses", 0)
        ev_wr = (s["ev_wins"] / ev_resolved * 100) if ev_resolved > 0 else 0
        lines.append(f"  <b>EV Bot:</b> {ev_total} trades ({s.get('ev_open', 0)} open) "
                     f"P&L: ${s.get('ev_pnl', 0):+,.2f} ({ev_wr:.0f}% WR)")

    # Weather bot breakdown
    weather_total = s.get("weather_total", 0)
    if weather_total:
        w_resolved = s.get("weather_wins", 0) + s.get("weather_losses", 0)
        w_wr = (s["weather_wins"] / w_resolved * 100) if w_resolved > 0 else 0
        lines.append(f"  <b>Weather Bot:</b> {weather_total} trades ({s.get('weather_open', 0)} open) "
                     f"P&L: ${s.get('weather_pnl', 0):+,.2f} ({w_wr:.0f}% WR)")

    return lines


# ---------------------------------------------------------------------------
# Signal alert builder
# ---------------------------------------------------------------------------

def _build_signal_report(signals: list[dict], portfolio: dict | None = None) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_sh = datetime.now(SHANGHAI_TZ).strftime("%H:%M Shanghai")
    now = f"{now_utc} ({now_sh})"

    entries = [s for s in signals if s.get("signal_type") == "entry"]
    exits = [s for s in signals if s.get("signal_type") == "exit"]

    lines = [
        f"<b>Trading Pipeline — Signal Alert</b>",
        f"{now}",
        "",
    ]

    if entries:
        lines.append(f"<b>ENTRY SIGNALS ({len(entries)})</b>")
        for s in entries:
            tag = "TREND" if "trend" in s.get("strategy", "") else "PA"
            lines.append(f"  [{tag}] {s['symbol']} LONG @ {s['price_at_signal']}")
        lines.append("")

    if exits:
        lines.append(f"<b>EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            tag = "TREND" if "trend" in s.get("strategy", "") else "PA"
            lines.append(f"  [{tag}] {s['symbol']} EXIT @ {s['price_at_signal']}")
        lines.append("")

    if not signals:
        lines.append("No signals today.")
        lines.append("")

    if portfolio:
        lines += [
            "<b>PORTFOLIO</b>",
            f"  Value: ${portfolio.get('portfolio_value', 0):,.0f}",
            f"  P&L: ${portfolio.get('total_pnl', 0):+,.0f} ({portfolio.get('return_pct', 0):+.1f}%)",
            f"  Open: {portfolio.get('open_positions', 0)} positions",
            f"  Win Rate: {portfolio.get('win_rate', 0):.0f}%",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    print(f"Telegram message sent. Status: {resp.status_code}")
