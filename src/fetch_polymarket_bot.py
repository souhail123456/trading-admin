"""Fetch trade data from the Polymarket bot repo via GitHub API."""

import json
from datetime import datetime, timezone

from github import Github


def fetch_polymarket_stats(gh: Github, repo_name: str) -> dict:
    """Pull logs/trades.jsonl from the Polymarket bot repo."""
    repo = gh.get_repo(repo_name)

    stats = {
        "source": "polymarket-bot",
        "repo": repo_name,
        "trades": [],
        "total_trades": 0,
        "today_trades": 0,
        "open_positions": 0,
        "total_pnl": 0.0,
        "win_count": 0,
        "loss_count": 0,
        "last_run": None,
    }

    # Fetch trades.jsonl
    try:
        trades_file = repo.get_contents("logs/trades.jsonl")
        content = trades_file.decoded_content.decode("utf-8")
        stats["trades"] = _parse_trades_jsonl(content)
    except Exception:
        stats["trades"] = []

    # Compute aggregate stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in stats["trades"]:
        stats["total_trades"] += 1
        trade_date = t.get("timestamp", "")[:10]
        if trade_date == today:
            stats["today_trades"] += 1
        pnl = t.get("pnl", 0.0)
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["win_count"] += 1
        elif pnl < 0:
            stats["loss_count"] += 1
        if t.get("status") == "open":
            stats["open_positions"] += 1

    # Last workflow run
    try:
        runs = repo.get_workflow_runs(status="completed")
        if runs.totalCount > 0:
            stats["last_run"] = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    return stats


def _parse_trades_jsonl(content: str) -> list[dict]:
    """Parse newline-delimited JSON trade log."""
    trades = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return trades
