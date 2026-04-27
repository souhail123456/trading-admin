"""Trading Admin Agent — orchestrates stock bot and polymarket bot reporting."""

import os
import sys

from github import Github

from fetch_stock_bot import fetch_stock_stats
from fetch_polymarket_bot import fetch_polymarket_stats
from telegram_report import send_daily_report

# Repo config — update the stock repo name to match your actual repo
STOCK_REPO = os.environ.get("STOCK_REPO", "souhail123456/stock-trading-bot")
POLYMARKET_REPO = os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot")


def main():
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        print("ERROR: GH_TOKEN not set")
        sys.exit(1)

    gh = Github(gh_token)

    print(f"Fetching stock bot stats from {STOCK_REPO}...")
    stock_stats = fetch_stock_stats(gh, STOCK_REPO)
    print(f"  -> {stock_stats['total_trades']} trades, P&L: ${stock_stats['total_pnl']:+,.2f}")

    print(f"Fetching polymarket bot stats from {POLYMARKET_REPO}...")
    poly_stats = fetch_polymarket_stats(gh, POLYMARKET_REPO)
    print(f"  -> {poly_stats['total_trades']} trades, P&L: ${poly_stats['total_pnl']:+,.2f}")

    print("Sending Telegram report...")
    send_daily_report(stock_stats, poly_stats)

    print("Done.")


if __name__ == "__main__":
    main()
