"""
Cross-Bot Risk Calculator
-------------------------
Reads positions from all 3 bots, computes aggregate exposure,
and writes risk limits to shared/global_state.json.

Usage:
    python3 -m pipeline.agents.cross_bot_risk
"""

import json
import os
import re
from datetime import datetime, timezone


def _read_fx_positions(db_path: str = None) -> list[dict]:
    """Read FX bot open positions from pipeline.db."""
    try:
        from pipeline.db import init_db
        conn = init_db(db_path)
        rows = conn.execute(
            "SELECT symbol, side, entry_price, quantity FROM paper_trades WHERE status = 'open'"
        ).fetchall()
        return [{"symbol": r["symbol"], "side": r["side"],
                 "value": abs(float(r["entry_price"]) * float(r["quantity"])),
                 "bot": "fx"} for r in rows]
    except Exception as e:
        print(f"  FX positions: {e}")
        return []


def _read_stock_positions() -> tuple[list[dict], float]:
    """Read stock bot positions via GitHub API. Returns (positions, portfolio_value)."""
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        return [], 0
    try:
        from github import Github
        gh = Github(gh_token)
        repo = gh.get_repo(os.environ.get("STOCK_REPO", "souhail123456/trading-bot"))
        content = repo.get_contents("memory/TRADE-LOG.md").decoded_content.decode()

        match = re.search(r"<!--\s*SUMMARY\s*\n(.*?)-->", content, re.DOTALL)
        if not match:
            return [], 0

        positions = []
        portfolio_value = 0
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "open_positions":
                for p in json.loads(value):
                    positions.append({
                        "symbol": p["symbol"], "side": p.get("side", "BUY"),
                        "value": p["shares"] * p["entry"],
                        "bot": "stock"
                    })
            elif key == "portfolio_value":
                portfolio_value = float(value)

        return positions, portfolio_value
    except Exception as e:
        print(f"  Stock positions: {e}")
        return [], 0


def _read_poly_positions() -> list[dict]:
    """Read polymarket bot active trades via GitHub API."""
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        return []
    try:
        from github import Github
        gh = Github(gh_token)
        repo = gh.get_repo(os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot"))
        content = repo.get_contents("logs/trades.jsonl").decoded_content.decode()

        active = []
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            trade = json.loads(line)
            if trade.get("status") == "open":
                active.append({
                    "symbol": trade.get("market", "?")[:30],
                    "side": trade.get("side", "YES"),
                    "value": float(trade.get("cost", 0)),
                    "bot": "poly"
                })
        return active
    except Exception as e:
        print(f"  Poly positions: {e}")
        return []


def compute_cross_bot_risk(db_path: str = None) -> dict:
    """Compute aggregate risk across all bots."""
    fx_pos = _read_fx_positions(db_path)
    stock_pos, stock_portfolio = _read_stock_positions()
    poly_pos = _read_poly_positions()

    all_positions = fx_pos + stock_pos + poly_pos

    # Aggregate by bot
    by_bot = {}
    for p in all_positions:
        bot = p["bot"]
        by_bot.setdefault(bot, {"count": 0, "total_value": 0})
        by_bot[bot]["count"] += 1
        by_bot[bot]["total_value"] += p["value"]

    total_exposure = sum(p["value"] for p in all_positions)
    total_positions = len(all_positions)

    # Detect overlapping symbols across bots
    symbols_by_bot = {}
    for p in all_positions:
        symbols_by_bot.setdefault(p["symbol"], set()).add(p["bot"])
    overlaps = {sym: list(bots) for sym, bots in symbols_by_bot.items() if len(bots) > 1}

    # Risk flags
    flags = []
    if total_positions > 10:
        flags.append("HIGH_POSITION_COUNT")
    if total_exposure > 150_000:
        flags.append("HIGH_TOTAL_EXPOSURE")
    if overlaps:
        flags.append(f"OVERLAP: {', '.join(overlaps.keys())}")

    result = {
        "total_positions": total_positions,
        "total_exposure": round(total_exposure, 2),
        "by_bot": by_bot,
        "overlaps": overlaps,
        "flags": flags,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print(f"Cross-bot risk: {total_positions} positions, ${total_exposure:,.0f} exposure")
    if flags:
        print(f"  Flags: {', '.join(flags)}")
    if overlaps:
        print(f"  Overlaps: {overlaps}")

    return result


def write_risk_to_shared(risk: dict):
    """Merge risk data into shared/global_state.json."""
    state_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "global_state.json")

    existing = {}
    if os.path.exists(state_path):
        with open(state_path) as f:
            existing = json.load(f)

    existing["cross_bot_risk"] = risk

    with open(state_path, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"Risk data written to {state_path}")


def main():
    risk = compute_cross_bot_risk()
    write_risk_to_shared(risk)


if __name__ == "__main__":
    main()
