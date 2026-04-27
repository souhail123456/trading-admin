"""
Trading Admin — Orchestrator
-----------------------------
Pulls stats from all 3 bots and sends a unified Telegram report.

Bots:
  1. Stock Bot (Alpaca) — TRADE-LOG.md via GitHub API
  2. FX Bot (Capital.com) — pipeline.db local
  3. Polymarket Bot — trades.jsonl via GitHub API

Usage:
    python main.py                    # full report
    python main.py --fx-only          # FX bot only (no GitHub needed)
"""

import argparse
import os
import sys

from fetch_fx_bot import fetch_fx_stats
from telegram_report import send_unified_report, send_fx_report


def main():
    parser = argparse.ArgumentParser(description="Trading Admin — Unified Report")
    parser.add_argument("--fx-only", action="store_true", help="FX bot report only")
    args = parser.parse_args()

    if args.fx_only:
        print("Fetching FX bot stats...")
        fx_stats = fetch_fx_stats()
        print(f"  -> {fx_stats['total_trades']} trades, P&L: ${fx_stats['total_pnl']:+,.2f}")
        print("Sending FX Telegram report...")
        send_fx_report(fx_stats)
        print("Done.")
        return

    # Full report — needs GitHub token
    gh_token = os.environ.get("GH_TOKEN")

    stock_stats = None
    poly_stats = None

    if gh_token:
        from github import Github
        gh = Github(gh_token)

        stock_repo = os.environ.get("STOCK_REPO", "souhail123456/trading-bot")  # repo name is trading-bot
        poly_repo = os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot")

        print(f"Fetching stock bot stats from {stock_repo}...")
        try:
            from fetch_stock_bot import fetch_stock_stats
            stock_stats = fetch_stock_stats(gh, stock_repo)
            print(f"  -> {stock_stats['total_trades']} trades, P&L: ${stock_stats['total_pnl']:+,.2f}")
        except Exception as e:
            print(f"  -> Failed: {e}")

        print(f"Fetching polymarket bot stats from {poly_repo}...")
        try:
            from fetch_polymarket_bot import fetch_polymarket_stats
            poly_stats = fetch_polymarket_stats(gh, poly_repo)
            print(f"  -> {poly_stats['total_trades']} trades, P&L: ${poly_stats['total_pnl']:+,.2f}")
        except Exception as e:
            print(f"  -> Failed: {e}")
    else:
        print("WARNING: GH_TOKEN not set — skipping stock + polymarket bots")

    print("Fetching FX bot stats...")
    fx_stats = fetch_fx_stats()
    print(f"  -> {fx_stats['total_trades']} trades, P&L: ${fx_stats['total_pnl']:+,.2f}")

    print("Sending unified Telegram report...")
    send_unified_report(stock_stats, fx_stats, poly_stats)
    print("Done.")


if __name__ == "__main__":
    main()
