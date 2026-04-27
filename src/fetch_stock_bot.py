"""Fetch trade data from the stock bot repo via GitHub API."""

import re
from datetime import datetime, timedelta, timezone

from github import Github


def fetch_stock_stats(gh: Github, repo_name: str) -> dict:
    """Pull TRADE-LOG.md and RESEARCH-LOG.md from the stock bot repo."""
    repo = gh.get_repo(repo_name)

    stats = {
        "source": "stock-bot",
        "repo": repo_name,
        "trades": [],
        "research": [],
        "total_trades": 0,
        "today_trades": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_pnl": 0.0,
        "last_run": None,
    }

    # Fetch TRADE-LOG.md
    try:
        trade_log = repo.get_contents("memory/TRADE-LOG.md")
        content = trade_log.decoded_content.decode("utf-8")
        stats["trades"] = _parse_trade_log(content)
    except Exception:
        stats["trades"] = []

    # Fetch RESEARCH-LOG.md
    try:
        research_log = repo.get_contents("memory/RESEARCH-LOG.md")
        content = research_log.decoded_content.decode("utf-8")
        stats["research"] = _parse_research_log(content)
    except Exception:
        stats["research"] = []

    # Compute aggregate stats
    today = datetime.now(timezone.utc).date()
    for t in stats["trades"]:
        stats["total_trades"] += 1
        if t.get("date") == str(today):
            stats["today_trades"] += 1
        pnl = t.get("pnl", 0.0)
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["win_count"] += 1
        elif pnl < 0:
            stats["loss_count"] += 1

    # Last workflow run
    try:
        runs = repo.get_workflow_runs(status="completed")
        if runs.totalCount > 0:
            stats["last_run"] = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    return stats


def _parse_trade_log(content: str) -> list[dict]:
    """Parse markdown trade log into structured data.

    Expects rows like: | 2026-04-25 | AAPL | BUY | 150.00 | 10 | +50.00 |
    """
    trades = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| Date") or line.startswith("|--"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 6:
            pnl_str = cols[5].replace("+", "").replace("$", "").strip()
            try:
                pnl = float(pnl_str)
            except ValueError:
                pnl = 0.0
            trades.append({
                "date": cols[0],
                "symbol": cols[1],
                "side": cols[2],
                "price": cols[3],
                "qty": cols[4],
                "pnl": pnl,
            })
    return trades


def _parse_research_log(content: str) -> list[dict]:
    """Parse research log entries."""
    entries = []
    current = None
    for line in content.splitlines():
        if line.startswith("## "):
            if current:
                entries.append(current)
            current = {"title": line[3:].strip(), "body": ""}
        elif current:
            current["body"] += line + "\n"
    if current:
        entries.append(current)
    return entries
