"""Fetch trade data from the Polymarket bot repo via GitHub API."""

import json
from datetime import datetime, timezone

from github import Github


def fetch_polymarket_stats(gh: Github, repo_name: str) -> dict:
    """Pull logs/trades.jsonl + logs/weather_trades.jsonl from the Polymarket bot repo."""
    repo = gh.get_repo(repo_name)

    stats = {
        "source": "polymarket-bot",
        "repo": repo_name,
        "trades": [],
        "weather_trades": [],
        "total_trades": 0,
        "today_trades": 0,
        "open_positions": 0,
        "total_pnl": 0.0,
        "win_count": 0,
        "loss_count": 0,
        # EV bot breakdown
        "ev_total": 0,
        "ev_open": 0,
        "ev_pnl": 0.0,
        "ev_wins": 0,
        "ev_losses": 0,
        # Weather bot breakdown
        "weather_total": 0,
        "weather_open": 0,
        "weather_pnl": 0.0,
        "weather_wins": 0,
        "weather_losses": 0,
        "last_run": None,
    }

    # Fetch EV trades
    try:
        trades_file = repo.get_contents("logs/trades.jsonl")
        content = trades_file.decoded_content.decode("utf-8")
        stats["trades"] = _parse_trades_jsonl(content)
    except Exception:
        stats["trades"] = []

    # Fetch weather trades
    try:
        weather_file = repo.get_contents("logs/weather_trades.jsonl")
        content = weather_file.decoded_content.decode("utf-8")
        stats["weather_trades"] = _parse_trades_jsonl(content)
    except Exception:
        stats["weather_trades"] = []

    # Compute per-category stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for t in stats["trades"]:
        _accumulate(stats, t, today, prefix="ev")

    for t in stats["weather_trades"]:
        _accumulate(stats, t, today, prefix="weather")

    # Totals across both
    stats["total_trades"] = stats["ev_total"] + stats["weather_total"]
    stats["open_positions"] = stats["ev_open"] + stats["weather_open"]
    stats["total_pnl"] = stats["ev_pnl"] + stats["weather_pnl"]
    stats["win_count"] = stats["ev_wins"] + stats["weather_wins"]
    stats["loss_count"] = stats["ev_losses"] + stats["weather_losses"]

    # Last workflow run, fallback to last commit
    try:
        runs = repo.get_workflow_runs(status="completed")
        if runs.totalCount > 0:
            stats["last_run"] = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    if not stats["last_run"]:
        try:
            commits = repo.get_commits()
            if commits.totalCount > 0:
                stats["last_run"] = commits[0].commit.author.date.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

    return stats


def _accumulate(stats: dict, trade: dict, today: str, prefix: str) -> None:
    """Accumulate stats for a single trade into the given prefix bucket."""
    stats[f"{prefix}_total"] += 1

    trade_date = (trade.get("date") or trade.get("timestamp", ""))[:10]
    if trade_date == today:
        stats["today_trades"] += 1

    resolved = trade.get("resolved", False)
    pnl = float(trade.get("realized_pnl", 0) or 0)

    if resolved:
        stats[f"{prefix}_pnl"] += pnl
        if trade.get("won"):
            stats[f"{prefix}_wins"] += 1
        else:
            stats[f"{prefix}_losses"] += 1
    else:
        stats[f"{prefix}_open"] += 1


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
