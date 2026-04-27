"""Fetch trade data from the stock bot repo via GitHub API."""

import json
import re
from datetime import datetime, timezone

from github import Github


def fetch_stock_stats(gh: Github, repo_name: str) -> dict:
    """Pull TRADE-LOG.md from the stock bot repo and parse the SUMMARY block."""
    repo = gh.get_repo(repo_name)

    stats = {
        "source": "stock-bot",
        "repo": repo_name,
        "trades": [],
        "open_positions": [],
        "total_trades": 0,
        "today_trades": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_pnl": 0.0,
        "portfolio_value": None,
        "cash": None,
        "last_run": None,
    }

    # Fetch TRADE-LOG.md
    try:
        trade_log = repo.get_contents("memory/TRADE-LOG.md")
        content = trade_log.decoded_content.decode("utf-8")
    except Exception:
        return stats

    # Parse machine-readable SUMMARY block
    summary = _parse_summary_block(content)
    if summary:
        stats["portfolio_value"] = summary.get("portfolio_value")
        stats["cash"] = summary.get("cash")
        stats["total_pnl"] = summary.get("total_pnl", 0.0)

        open_pos = summary.get("open_positions", [])
        stats["open_positions"] = open_pos
        stats["total_trades"] = len(open_pos)

        closed = summary.get("closed_trades", [])
        stats["trades"] = closed
        stats["total_trades"] += len(closed)

        for t in closed:
            pnl = t.get("realized_pnl", 0)
            if pnl > 0:
                stats["win_count"] += 1
            elif pnl < 0:
                stats["loss_count"] += 1

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


def _parse_summary_block(content: str) -> dict | None:
    """Parse the <!-- SUMMARY ... --> block from TRADE-LOG.md."""
    match = re.search(r"<!--\s*SUMMARY\s*\n(.*?)-->", content, re.DOTALL)
    if not match:
        return None

    summary = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key in ("portfolio_value", "cash", "total_pnl"):
            try:
                summary[key] = float(value)
            except ValueError:
                pass
        elif key in ("open_positions", "closed_trades"):
            try:
                summary[key] = json.loads(value)
            except json.JSONDecodeError:
                summary[key] = []
        elif key == "last_updated":
            summary[key] = value

    return summary
