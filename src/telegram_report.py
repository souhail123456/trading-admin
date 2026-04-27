"""Build and send a unified Telegram dashboard report."""

import os
from datetime import datetime, timezone

import requests


def send_daily_report(stock_stats: dict, poly_stats: dict) -> None:
    """Format and send the combined daily report to Telegram."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    msg = _build_report(stock_stats, poly_stats)
    _send_telegram(bot_token, chat_id, msg)


def send_signal_alert(signals: list[dict], portfolio: dict | None = None) -> None:
    """Send trading signal alerts to Telegram."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    msg = _build_signal_report(signals, portfolio)
    _send_telegram(bot_token, chat_id, msg)


def _build_signal_report(signals: list[dict], portfolio: dict | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
            lines.append(f"  [{tag}] {s['symbol']} LONG @ ${s['price_at_signal']}")
        lines.append("")

    if exits:
        lines.append(f"<b>EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            tag = "TREND" if "trend" in s.get("strategy", "") else "PA"
            lines.append(f"  [{tag}] {s['symbol']} EXIT @ ${s['price_at_signal']}")
        lines.append("")

    if not signals:
        lines.append("No signals today. Hold or stay cash.")
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


def _build_report(stock: dict, poly: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_pnl = stock["total_pnl"] + poly["total_pnl"]
    total_trades = stock["total_trades"] + poly["total_trades"]
    today_trades = stock["today_trades"] + poly["today_trades"]
    total_wins = stock["win_count"] + poly["win_count"]
    total_losses = stock["loss_count"] + poly["loss_count"]
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

    lines = [
        f"📊 <b>Trading Admin — Daily Report</b>",
        f"🕐 {now}",
        "",
        f"<b>═══ COMBINED OVERVIEW ═══</b>",
        f"Total P&L: <b>${total_pnl:+,.2f}</b>",
        f"Trades: {total_trades} total ({today_trades} today)",
        f"Win rate: {win_rate:.0f}% ({total_wins}W / {total_losses}L)",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>📈 Stock Bot</b> ({stock['repo']})",
        f"  P&L: ${stock['total_pnl']:+,.2f}",
        f"  Trades: {stock['total_trades']} ({stock['today_trades']} today)",
        f"  W/L: {stock['win_count']}W / {stock['loss_count']}L",
        f"  Last run: {stock['last_run'] or 'N/A'}",
    ]

    # Recent stock trades
    recent_stock = stock["trades"][-5:]
    if recent_stock:
        lines.append("  Recent:")
        for t in reversed(recent_stock):
            lines.append(
                f"    {t['symbol']} {t['side']} {t['qty']}@{t['price']} "
                f"P&L: ${t['pnl']:+,.2f}"
            )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>🔮 Polymarket Bot</b> ({poly['repo']})",
        f"  P&L: ${poly['total_pnl']:+,.2f}",
        f"  Trades: {poly['total_trades']} ({poly['today_trades']} today)",
        f"  Open positions: {poly['open_positions']}",
        f"  W/L: {poly['win_count']}W / {poly['loss_count']}L",
        f"  Last run: {poly['last_run'] or 'N/A'}",
    ]

    # Recent polymarket trades
    recent_poly = poly["trades"][-5:]
    if recent_poly:
        lines.append("  Recent:")
        for t in reversed(recent_poly):
            market = t.get("market", t.get("question", "?"))[:40]
            side = t.get("side", t.get("outcome", "?"))
            pnl = t.get("pnl", 0.0)
            lines.append(f"    {market} [{side}] P&L: ${pnl:+,.2f}")

    # Research highlights from stock bot
    if stock.get("research"):
        lines += ["", "<b>🔬 Latest Research</b>"]
        for r in stock["research"][-3:]:
            lines.append(f"  • {r['title']}")

    return "\n".join(lines)


def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    print(f"Telegram message sent. Status: {resp.status_code}")
