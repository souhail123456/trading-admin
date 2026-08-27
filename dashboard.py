"""
Trading Admin — Unified Dashboard
----------------------------------
Web dashboard covering all 3 bots with charts and live data.

Data sources (in priority order, each with graceful fallback):
  FX      -> live Capital.com broker, else shared/pipeline.db (paper_trades) +
             shared/daily_history.jsonl (fx daily_snapshot events)
  Stock   -> GitHub API (trading-bot repo), else shared/daily_history.jsonl
             (stock daily_snapshot events)
  Weather -> GitHub API (polymarket-bot repo), else local repo clone at
             work/polymarket-bot/logs/, else shared/daily_history.jsonl
             (polymarket trade events)
  Global  -> shared/global_state.json (regime, VIX, cross-bot risk)

Modes:
  --serve       Local server with live data (http://localhost:8050)
  --static      Generate static HTML for GitHub Pages deployment

Usage:
    python3 dashboard.py --serve
    python3 dashboard.py --static --output docs/index.html
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

try:
    from pipeline.agents.fx_pipeline import estimate_swap_cost as _estimate_swap_cost
except Exception:
    _estimate_swap_cost = None

SHARED_DIR = Path(__file__).parent / "shared"
WORK_DIR = Path(__file__).parent / "work"


def _us_market_open() -> bool:
    """Check if US stock market is currently open (9:30-16:00 ET, Mon-Fri)."""
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:  # Sat/Sun
        return False
    t = et.time()
    from datetime import time as dtime
    return dtime(9, 30) <= t <= dtime(16, 0)


# ---------------------------------------------------------------------------
# Local file helpers (shared/ + work/ are the on-disk source of truth written
# by the daily pipeline; GitHub API is used opportunistically when a token is
# available, local files are the fallback so the dashboard always renders).
# ---------------------------------------------------------------------------

def _read_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _connect_shared_db() -> sqlite3.Connection | None:
    """Read-only connection to shared/pipeline.db (the committed, real DB)."""
    path = SHARED_DIR / "pipeline.db"
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        try:
            conn = sqlite3.connect(str(path))
        except Exception:
            return None
    conn.row_factory = sqlite3.Row
    return conn


def get_global_state() -> dict:
    """regime, VIX, cross-bot risk — committed daily by the pipeline."""
    return _read_json(SHARED_DIR / "global_state.json", {}) or {}


def load_daily_history() -> dict:
    """Parse shared/daily_history.jsonl once into per-bot buckets, oldest first."""
    stock, fx, poly = [], [], []
    for d in _read_jsonl(SHARED_DIR / "daily_history.jsonl"):
        event, bot = d.get("event"), d.get("bot")
        if event == "daily_snapshot" and bot == "stock":
            stock.append(d)
        elif event == "daily_snapshot" and bot == "fx":
            fx.append(d)
        elif event == "trade" and bot == "polymarket":
            poly.append(d)
    stock.sort(key=lambda d: d.get("timestamp", ""))
    fx.sort(key=lambda d: d.get("timestamp", ""))
    poly.sort(key=lambda d: d.get("timestamp", ""))
    return {"stock_snapshots": stock, "fx_snapshots": fx, "poly_trades": poly}


def _dedupe_daily(snaps: list[dict], value_key: str) -> list[dict]:
    """Collapse multiple same-day snapshots to the last one; return [{time, value}]."""
    seen = {}
    for s in snaps:
        ts = s.get("timestamp", "")
        if not ts:
            continue
        v = s.get(value_key)
        if v is not None:
            seen[ts[:10]] = v
    return [{"time": d, "value": v} for d, v in sorted(seen.items())]


def _fx_swap_and_days(symbol: str, side: str, entry_price, quantity, opened_at) -> tuple[int | None, float | None]:
    """Days held + estimated overnight swap cost for an open FX position."""
    days_held = None
    if opened_at:
        try:
            d0 = datetime.strptime(str(opened_at)[:19], "%Y-%m-%dT%H:%M:%S")
            days_held = max((datetime.now() - d0).days, 0)
        except Exception:
            days_held = None
    swap = None
    if days_held is not None and _estimate_swap_cost is not None:
        try:
            swap = _estimate_swap_cost(symbol, str(side).lower(), float(entry_price), float(quantity or 1), days_held)
        except Exception:
            swap = None
    return days_held, swap


def stats(trades: list[dict]) -> dict:
    """Basic win/loss/pnl summary for a list of polymarket-style trade dicts."""
    resolved = [t for t in trades if t.get("resolved")]
    wins = [t for t in resolved if t.get("won")]
    pnl = sum(float(t.get("realized_pnl", 0) or 0) for t in resolved)
    risked = sum(float(t.get("size_usd", 0) or 0) for t in trades)
    return {
        "total": len(trades),
        "open": len([t for t in trades if not t.get("resolved")]),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(resolved) - len(wins),
        "win_rate": round(len(wins) / len(resolved) * 100) if resolved else 0,
        "pnl": round(pnl, 2),
        "risked": round(risked, 2),
    }


def weather_extra_stats(trades: list[dict]) -> dict:
    """Avg win/loss and P&L split by side (YES/NO) for the weather strategy."""
    resolved = [t for t in trades if t.get("resolved")]
    wins = [t for t in resolved if t.get("won")]
    losses = [t for t in resolved if not t.get("won")]
    avg_win = sum(float(t.get("realized_pnl", 0) or 0) for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(float(t.get("realized_pnl", 0) or 0) for t in losses) / len(losses) if losses else 0.0

    def side_stats(side: str) -> dict:
        lst = [t for t in resolved if (t.get("side") or "").upper() == side]
        pnl = sum(float(t.get("realized_pnl", 0) or 0) for t in lst)
        w = len([t for t in lst if t.get("won")])
        return {"count": len(lst), "pnl": round(pnl, 2), "win_rate": round(w / len(lst) * 100) if lst else 0}

    return {
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "yes": side_stats("YES"),
        "no": side_stats("NO"),
    }


def _normalize_daily_history_trade(t: dict) -> dict:
    """Map a shared/daily_history.jsonl polymarket trade event to the
    weather_trades.jsonl / trades.jsonl schema so stats()/weather_extra_stats()
    work unchanged regardless of which source fed them."""
    return {
        "market": t.get("symbol"),
        "question": t.get("symbol"),
        "side": t.get("side"),
        "size_usd": t.get("size_usd", 0),
        "realized_pnl": t.get("pnl", 0),
        "resolved": t.get("status") == "resolved",
        "won": t.get("won"),
        "timestamp": t.get("timestamp"),
    }


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def get_fx_data() -> dict:
    """FX: live Capital.com positions/account when reachable, always enriched
    with realized P&L from shared/pipeline.db and swap-cost estimates; falls
    back to the last shared/daily_history.jsonl snapshot when the broker is
    unreachable (e.g. running locally without credentials)."""
    from pipeline.agents.broker_capital import CapitalBroker

    open_trades = []
    account = {}
    unrealized_pnl = 0.0
    market_open = False
    live = False

    try:
        broker = CapitalBroker()
        account = broker.get_account()
        positions = broker.get_positions()
        unrealized_pnl = account.get("unrealized_pnl", 0.0)
        market_open = broker.is_market_open()
        live = bool(account)

        for p in positions:
            open_trades.append({
                "symbol": p["epic"],
                "side": p["direction"],
                "quantity": p["size"],
                "entry_price": p["entry_price"],
                "unrealized_pnl": p["unrealized_pnl"],
                "opened_at": p.get("created_date", ""),
                "deal_id": p.get("deal_id", ""),
                "stop_level": p.get("stop_level"),
                "profit_level": p.get("profit_level"),
            })

        broker.disconnect()
    except Exception as e:
        print(f"  Capital.com fetch failed: {e}")

    # opened_at fallback lookup, keyed by symbol (from the pipeline's own trade log)
    opened_at_by_symbol = {
        t["symbol"]: t.get("opened_at")
        for t in _read_jsonl(SHARED_DIR / "fx_trades.jsonl")
        if t.get("status") == "open"
    }

    if not open_trades:
        # No live broker connection — reconstruct from the last committed snapshot.
        dh = load_daily_history()
        if dh["fx_snapshots"]:
            latest = dh["fx_snapshots"][-1]
            for p in latest.get("open_positions", []):
                sym = p.get("symbol")
                open_trades.append({
                    "symbol": sym,
                    "side": p.get("side", "long"),
                    "quantity": p.get("quantity", 0),
                    "entry_price": p.get("entry", 0),
                    "unrealized_pnl": None,  # no live price available offline
                    "opened_at": opened_at_by_symbol.get(sym, ""),
                    "deal_id": "",
                    "stop_level": None,
                    "profit_level": None,
                })
    else:
        for t in open_trades:
            if not t.get("opened_at"):
                t["opened_at"] = opened_at_by_symbol.get(t["symbol"], "")

    total_swap = 0.0
    for t in open_trades:
        days_held, swap = _fx_swap_and_days(t["symbol"], t["side"], t["entry_price"], t["quantity"], t["opened_at"])
        t["days_held"] = days_held
        t["swap_est"] = swap
        if swap is not None:
            total_swap += swap

    # Realized P&L + win rate from the real, committed DB (shared/pipeline.db).
    realized_pnl = 0.0
    closed_count = 0
    win_count = 0
    dbconn = _connect_shared_db()
    if dbconn is not None:
        try:
            rows = dbconn.execute("SELECT pnl FROM paper_trades WHERE status = 'closed'").fetchall()
            closed_count = len(rows)
            for r in rows:
                pnl = r["pnl"] or 0.0
                realized_pnl += pnl
                if pnl > 0:
                    win_count += 1
        except Exception as e:
            print(f"  shared/pipeline.db read failed: {e}")
        finally:
            dbconn.close()

    state = get_global_state()
    regime = {
        "regime": state.get("regime", "N/A"),
        "vix": state.get("vix"),
        "description": state.get("recommendations", {}).get("100", {}).get("reason", ""),
    }

    return {
        "open_trades": open_trades,
        "realized_pnl": round(realized_pnl, 2),
        "closed_count": closed_count,
        "win_rate": round(win_count / closed_count * 100) if closed_count else 0,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_swap_est": round(total_swap, 2),
        "market_open": market_open,
        "open_count": len(open_trades),
        "live": live,
        "regime": regime,
        "account": account,
    }


import re as _re

_SUMMARY_RE = _re.compile(r"<!--\s*SUMMARY\s*\n(.*?)-->", _re.DOTALL)


def _parse_trade_log_summary(content: str) -> dict:
    """Extract the machine-readable SUMMARY block from TRADE-LOG.md."""
    match = _SUMMARY_RE.search(content)
    if not match:
        return {}
    summary = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in ("portfolio_value", "cash", "total_pnl"):
            summary[key] = float(value)
        elif key in ("open_positions", "closed_trades"):
            summary[key] = json.loads(value)
        elif key == "last_updated":
            summary[key] = value
    return summary


def _fetch_stock_via_github() -> dict | None:
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        return None
    try:
        from github import Github
        gh = Github(gh_token)
        repo = gh.get_repo(os.environ.get("STOCK_REPO", "souhail123456/trading-bot"))
        content = repo.get_contents("memory/TRADE-LOG.md").decoded_content.decode()
        summary = _parse_trade_log_summary(content)
        if not summary:
            return None

        last_run = None
        try:
            runs = repo.get_workflow_runs(status="completed")
            if runs.totalCount > 0:
                last_run = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

        equity_history = []
        try:
            csv_content = repo.get_contents("memory/PORTFOLIO-HISTORY.csv").decoded_content.decode()
            seen_dates = {}
            for line in csv_content.splitlines()[1:]:  # skip header
                parts = line.split(",")
                if len(parts) >= 2:
                    ts = parts[0].strip()[:10]  # date only
                    eq = float(parts[1])
                    seen_dates[ts] = eq  # last entry per day wins
            equity_history = [{"time": d, "value": v} for d, v in sorted(seen_dates.items())]
        except Exception:
            pass

        return {
            "portfolio_value": summary.get("portfolio_value", 100000),
            "cash": summary.get("cash", 100000),
            "total_pnl": summary.get("total_pnl", 0),
            "open_positions": summary.get("open_positions", []),
            "closed_trades": summary.get("closed_trades", []),
            "last_run": last_run,
            "equity_history": equity_history,
            "source": "GitHub API (live)",
        }
    except Exception as e:
        print(f"  Stock data fetch failed: {e}")
        return None


def _is_stale_stock_snap(s: dict) -> bool:
    """Detect a degenerate 'positions not priced' stock snapshot.

    Root cause of the equity-curve flatline (late-May..mid-June 2026): on days the
    stock bot could not price its holdings it still wrote a daily_snapshot, but with
    equity collapsed to exactly its (frozen) cash while `positions` was non-empty.
    That single frozen number ($100,659.65) was then carried forward for ~4 weeks
    and drawn as if it were real daily equity. A genuine snapshot with open
    positions always has equity != cash (positions carry market value); a truly
    flat book has 0 positions (equity == cash is then legitimate and kept). So the
    stale signature is: positions present AND equity == cash to the cent.
    """
    eq, cash = s.get("equity"), s.get("cash")
    positions = s.get("positions") or []
    return (
        eq is not None and cash is not None and bool(positions)
        and abs(float(eq) - float(cash)) < 0.01
    )


def _fetch_stock_local() -> dict | None:
    """Fallback: shared/daily_history.jsonl stock daily_snapshot events (always
    committed by the pipeline), optionally enriched by a local trading-bot clone."""
    dh = load_daily_history()
    snaps = dh["stock_snapshots"]
    if not snaps:
        return None

    # Drop stale/degenerate snapshots so carried-forward equity is never drawn as
    # real data (see _is_stale_stock_snap). Keep the original list if filtering
    # would leave us with nothing to show.
    clean = [s for s in snaps if not _is_stale_stock_snap(s)]
    if clean:
        snaps = clean

    latest = snaps[-1]
    equity_history = _dedupe_daily(snaps, "equity")

    positions = []
    for p in latest.get("positions", []):
        positions.append({
            "symbol": p.get("symbol"),
            "side": "BUY" if str(p.get("side", "long")).lower() == "long" else "SELL",
            "shares": p.get("qty", 0),
            "entry": p.get("entry", 0),
            "unrealized_pnl": p.get("unrealized_pnl", 0),
        })

    closed_trades = []
    last_run = None
    local_log = WORK_DIR / "trading-bot" / "memory" / "TRADE-LOG.md"
    if local_log.exists():
        try:
            summary = _parse_trade_log_summary(local_log.read_text())
            closed_trades = summary.get("closed_trades", [])
            last_run = summary.get("last_updated")
        except Exception:
            pass

    equity = latest.get("equity", 100000)
    return {
        "portfolio_value": equity,
        "cash": latest.get("cash", 0),
        "total_pnl": round(equity - 100000, 2),
        "open_positions": positions,
        "closed_trades": closed_trades,
        "last_run": last_run,
        "equity_history": equity_history,
        "source": "shared/daily_history.jsonl (local)",
    }


def get_stock_data() -> dict | None:
    return _fetch_stock_via_github() or _fetch_stock_local()


def _fetch_poly_via_github() -> dict | None:
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        return None
    try:
        from github import Github
        gh = Github(gh_token)
        repo = gh.get_repo(os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot"))

        def parse_jsonl(path):
            try:
                content = repo.get_contents(path).decoded_content.decode()
                return [json.loads(l) for l in content.splitlines() if l.strip()]
            except Exception:
                return []

        ev_trades = parse_jsonl("logs/trades.jsonl")
        weather_trades = parse_jsonl("logs/weather_trades.jsonl")
        if not ev_trades and not weather_trades:
            return None

        last_run = None
        try:
            runs = repo.get_workflow_runs(status="completed")
            if runs.totalCount > 0:
                last_run = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

        return {
            "ev": stats(ev_trades),
            "weather": stats(weather_trades),
            "weather_extra": weather_extra_stats(weather_trades),
            "ev_recent": ev_trades[-5:],
            "weather_recent": weather_trades[-5:],
            "last_run": last_run,
            "source": "GitHub API (live)",
        }
    except Exception as e:
        print(f"  Polymarket data fetch failed: {e}")
        return None


def _fetch_poly_local() -> dict | None:
    """Fallback chain: local polymarket-bot clone (work/) -> shared/daily_history.jsonl."""
    weather_trades = _read_jsonl(WORK_DIR / "polymarket-bot" / "logs" / "weather_trades.jsonl")
    ev_trades = _read_jsonl(WORK_DIR / "polymarket-bot" / "logs" / "trades.jsonl")
    source = "local polymarket-bot clone"

    if not weather_trades and not ev_trades:
        dh = load_daily_history()
        normalized = [_normalize_daily_history_trade(t) for t in dh["poly_trades"]]
        weather_trades = [t for t, raw in zip(normalized, dh["poly_trades"]) if raw.get("strategy") == "weather"]
        ev_trades = [t for t, raw in zip(normalized, dh["poly_trades"]) if raw.get("strategy") == "ev"]
        source = "shared/daily_history.jsonl (local)"

    if not weather_trades and not ev_trades:
        return None

    return {
        "ev": stats(ev_trades),
        "weather": stats(weather_trades),
        "weather_extra": weather_extra_stats(weather_trades),
        "ev_recent": ev_trades[-5:],
        "weather_recent": weather_trades[-5:],
        "last_run": None,
        "source": source,
    }


def get_poly_data() -> dict | None:
    return _fetch_poly_via_github() or _fetch_poly_local()


# ---------------------------------------------------------------------------
# Index Trend (dry-run) — reads the same shared/pipeline.db the rest of the
# dashboard uses. Strategies 200/201/202 are logged by
# pipeline.agents.index_pipeline (signals table + status='intent' paper_trades).
# No live orders are ever placed by this path — it is paper/dry-run only.
# ---------------------------------------------------------------------------

INDEX_STRATEGIES = [
    {"strategy_id": 200, "name": "S&P 500", "symbol": "US500"},
    {"strategy_id": 201, "name": "Nasdaq 100", "symbol": "US100"},
    {"strategy_id": 202, "name": "Gold", "symbol": "GOLD"},
]


def get_index_data() -> dict | None:
    """Index-trend dry-run state from shared/pipeline.db.

    Returns per-strategy latest signal (+ decoded full_state), any open dry-run
    intent positions, and a short recent-signal log. None if the DB is missing
    or has no index signals yet."""
    conn = _connect_shared_db()
    if conn is None:
        return None
    try:
        strategies = []
        for meta in INDEX_STRATEGIES:
            sid = meta["strategy_id"]
            try:
                row = conn.execute(
                    "SELECT signal_type, symbol, price_at_signal, full_state, generated_at "
                    "FROM signals WHERE strategy_id = ? ORDER BY generated_at DESC, id DESC LIMIT 1",
                    (sid,),
                ).fetchone()
            except Exception:
                return None
            if row is None:
                continue
            try:
                fs = json.loads(row["full_state"]) if row["full_state"] else {}
            except Exception:
                fs = {}
            strategies.append({
                "strategy_id": sid,
                "name": meta["name"],
                "symbol": row["symbol"] or meta["symbol"],
                "signal_type": row["signal_type"],
                "action": fs.get("action") or row["signal_type"],
                "price": row["price_at_signal"],
                "generated_at": row["generated_at"],
                "state": fs,
            })

        if not strategies:
            return None

        # Open dry-run intent positions (status='intent'). The pipeline may log a
        # fresh intent each run while flat, so keep only the newest per strategy.
        intents_by_strat: dict[int, dict] = {}
        try:
            for r in conn.execute(
                "SELECT strategy_id, symbol, side, entry_price, quantity, stop_loss, thesis, created_at "
                "FROM paper_trades WHERE status = 'intent' AND strategy_id IN (200, 201, 202) "
                "ORDER BY created_at DESC, id DESC"
            ).fetchall():
                sid = r["strategy_id"]
                if sid not in intents_by_strat:
                    intents_by_strat[sid] = {
                        "strategy_id": sid,
                        "symbol": r["symbol"],
                        "side": r["side"],
                        "entry_price": r["entry_price"],
                        "quantity": r["quantity"],
                        "stop_loss": r["stop_loss"],
                        "thesis": r["thesis"],
                        "created_at": r["created_at"],
                    }
        except Exception:
            pass
        intents = list(intents_by_strat.values())

        # Recent signal log across the three strategies.
        recent = []
        try:
            name_by_sid = {m["strategy_id"]: m["name"] for m in INDEX_STRATEGIES}
            for r in conn.execute(
                "SELECT strategy_id, signal_type, symbol, price_at_signal, full_state, generated_at "
                "FROM signals WHERE strategy_id IN (200, 201, 202) "
                "ORDER BY generated_at DESC, id DESC LIMIT 12"
            ).fetchall():
                try:
                    fs = json.loads(r["full_state"]) if r["full_state"] else {}
                except Exception:
                    fs = {}
                recent.append({
                    "name": name_by_sid.get(r["strategy_id"], r["symbol"]),
                    "symbol": r["symbol"],
                    "action": fs.get("action") or r["signal_type"],
                    "price": r["price_at_signal"],
                    "generated_at": r["generated_at"],
                })
        except Exception:
            pass

        return {"strategies": strategies, "intents": intents, "recent": recent}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Combined Total Balance (hero) — stock equity + FX balance + poly balance,
# plus a combined daily series for the day-over-day change.
#
# Balance sources (robust for the static/offline build):
#   Stock : latest portfolio equity (get_stock_data; already stale-filtered).
#   FX    : live broker equity if reachable & non-zero, else FX_ACCOUNT_BALANCE
#           (default 300) + realized P&L of closed strategy-100/101 paper_trades.
#   Poly  : each strategy book starts at POLY_BANKROLL (default 100 — the value
#           found in work/polymarket-bot/{weather,econ,crypto}_bot.py as
#           starting_bankroll=100.0). Per-book balance = max(0, base + that
#           book's net realized P&L) — floored because a paper bankroll cannot go
#           negative — summed across the live strategy books (econ/ev/weather).
# ---------------------------------------------------------------------------

FX_ACCOUNT_BASE = float(os.environ.get("FX_ACCOUNT_BALANCE", "300"))
POLY_BANKROLL_PER_BOOK = float(os.environ.get("POLY_BANKROLL", "100"))


def _poly_realized_by_strategy(poly_trades: list[dict]) -> dict[str, float]:
    """Net realized P&L per poly strategy from daily_history poly trade events."""
    out: dict[str, float] = {}
    for t in poly_trades:
        strat = t.get("strategy") or "poly"
        out.setdefault(strat, 0.0)
        if t.get("status") in ("resolved", "closed"):
            out[strat] += float(t.get("pnl", 0) or 0)
    return out


def _poly_balance_from_realized(realized_by_strat: dict[str, float]) -> float:
    """Sum of per-book balances, each floored at 0 (can't lose past the bankroll)."""
    if not realized_by_strat:
        return POLY_BANKROLL_PER_BOOK
    return sum(max(0.0, POLY_BANKROLL_PER_BOOK + pnl) for pnl in realized_by_strat.values())


def get_total_balance(fx: dict, stock: dict | None) -> dict:
    """Combined balance across all three bots + day-over-day change.

    Returns per-bot balances (never 0/None-poisoned), the grand total, a combined
    daily series (one point per date, each bot carried forward on gap days), and
    the absolute/percent change vs the previous day."""
    dh = load_daily_history()

    # ---- Stock balance (latest equity) ----
    stock_bal = None
    if stock and isinstance(stock.get("portfolio_value"), (int, float)):
        stock_bal = float(stock["portfolio_value"])
    if stock_bal is None:
        # last resort: newest non-stale stock snapshot equity
        for s in reversed(dh["stock_snapshots"]):
            if not _is_stale_stock_snap(s) and isinstance(s.get("equity"), (int, float)):
                stock_bal = float(s["equity"])
                break
    if stock_bal is None:
        stock_bal = 0.0

    # ---- FX balance (prefer live equity; else base + realized) ----
    live_equity = (fx.get("account") or {}).get("equity")
    if isinstance(live_equity, (int, float)) and live_equity:
        fx_bal = float(live_equity)
    else:
        fx_bal = FX_ACCOUNT_BASE + float(fx.get("realized_pnl", 0) or 0)

    # ---- Poly balance (per-book floored, from daily_history) ----
    poly_realized = _poly_realized_by_strategy(dh["poly_trades"])
    poly_bal = _poly_balance_from_realized(poly_realized)

    total = stock_bal + fx_bal + poly_bal

    # ---- Combined daily series (carry-forward each bot per date) ----
    # Per-day stock equity (skip stale/degenerate snapshots).
    stock_by_day: dict[str, float] = {}
    for s in dh["stock_snapshots"]:
        if _is_stale_stock_snap(s):
            continue
        ts = s.get("timestamp", "")
        if ts and isinstance(s.get("equity"), (int, float)):
            stock_by_day[ts[:10]] = float(s["equity"])

    # Per-day FX balance = base + that day's total realized P&L.
    fx_by_day: dict[str, float] = {}
    for s in dh["fx_snapshots"]:
        ts = s.get("timestamp", "")
        if ts:
            rp = s.get("total_realized_pnl")
            fx_by_day[ts[:10]] = FX_ACCOUNT_BASE + float(rp or 0)

    # Per-day poly balance = floored per-book sum using cumulative realized to date.
    poly_by_day: dict[str, float] = {}
    poly_cum: dict[str, float] = {k: 0.0 for k in poly_realized}
    for t in sorted(dh["poly_trades"], key=lambda x: x.get("timestamp", "")):
        strat = t.get("strategy") or "poly"
        poly_cum.setdefault(strat, 0.0)
        if t.get("status") in ("resolved", "closed"):
            poly_cum[strat] += float(t.get("pnl", 0) or 0)
        ts = t.get("timestamp", "")
        if ts:
            poly_by_day[ts[:10]] = _poly_balance_from_realized(poly_cum)

    all_days = sorted(set(stock_by_day) | set(fx_by_day) | set(poly_by_day))
    n_books = max(len(poly_realized), 1)
    series: list[dict] = []
    ls = lf = lp = None
    for d in all_days:
        if d in stock_by_day:
            ls = stock_by_day[d]
        if d in fx_by_day:
            lf = fx_by_day[d]
        if d in poly_by_day:
            lp = poly_by_day[d]
        # Defaults before a bot's first snapshot: FX/poly at their bankroll bases,
        # stock at 0 (it has the earliest, densest history so this rarely bites).
        sv = ls if ls is not None else 0.0
        fv = lf if lf is not None else FX_ACCOUNT_BASE
        pv = lp if lp is not None else POLY_BANKROLL_PER_BOOK * n_books
        series.append({"time": d, "value": round(sv + fv + pv, 2)})

    delta_abs = delta_pct = None
    if len(series) >= 2:
        prev, cur = series[-2]["value"], series[-1]["value"]
        delta_abs = round(cur - prev, 2)
        delta_pct = round((delta_abs / prev * 100), 2) if prev else None

    return {
        "total": round(total, 2),
        "stock": round(stock_bal, 2),
        "fx": round(fx_bal, 2),
        "poly": round(poly_bal, 2),
        "fx_live": bool(isinstance(live_equity, (int, float)) and live_equity),
        "poly_books": n_books,
        "series": series,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
    }


# ---------------------------------------------------------------------------
# Inline SVG / CSS chart builders (no external JS libraries)
# ---------------------------------------------------------------------------

def render_equity_svg(history: list[dict], baseline: float = 100000,
                      width: int = 1120, height: int = 300) -> str:
    """Stock portfolio equity curve — self-contained inline SVG line+area chart.

    Styled per the dataviz skill (dark surface): single blue series (slot-1
    #3987e5), recessive hairline grid, muted axis ink, 2px line, dashed baseline,
    a single direct-labelled last point, and a JS crosshair+tooltip (the default
    interaction layer for a line chart). No external libraries."""
    if not history or len(history) < 2:
        return '<div class="empty" style="padding:72px 0">No equity history available yet</div>'

    # Colors (validated dark-mode dataviz palette)
    C_SERIES = "#3987e5"      # categorical slot 1 (blue)
    C_GRID = "#232a31"        # hairline gridline (dark)
    C_AXIS = "#3a4553"        # baseline / axis
    C_MUTED = "#7a8698"       # axis + tick ink
    C_INK = "#e6eaf0"         # primary ink (direct label)
    C_GOOD = "#22c55e"
    C_BAD = "#ef4444"

    values = [h["value"] for h in history]
    n = len(values)
    lo, hi = min(values + [baseline]), max(values + [baseline])
    pad = (hi - lo) * 0.10 or max(hi * 0.02, 1)
    lo, hi = lo - pad, hi + pad

    pad_l, pad_r, pad_t, pad_b = 64, 66, 18, 30
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i):
        return pad_l + (i / (n - 1)) * plot_w

    def y(v):
        return pad_t + (1 - (v - lo) / (hi - lo)) * plot_h

    pts = [(x(i), y(v)) for i, v in enumerate(values)]
    path_d = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area_d = path_d + f" L {pts[-1][0]:.1f},{pad_t + plot_h:.1f} L {pts[0][0]:.1f},{pad_t + plot_h:.1f} Z"
    baseline_y = y(baseline)

    grid = []
    for i in range(5):
        gy = pad_t + plot_h * i / 4
        val = hi - (hi - lo) * i / 4
        grid.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" stroke="{C_GRID}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l - 10}" y="{gy + 3.5:.1f}" text-anchor="end" font-size="11" fill="{C_MUTED}" style="font-variant-numeric:tabular-nums">${val:,.0f}</text>')

    x_labels = []
    for idx in sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1}):
        anchor = "start" if idx == 0 else "end" if idx == n - 1 else "middle"
        x_labels.append(f'<text x="{x(idx):.1f}" y="{height - 8}" text-anchor="{anchor}" font-size="11" fill="{C_MUTED}">{history[idx]["time"]}</text>')

    # Direct-labelled last point (selective — never a label on every point)
    last_v = values[-1]
    last_col = C_GOOD if last_v >= baseline else C_BAD
    lx, ly = pts[-1]
    last_label = (
        f'<line x1="{lx:.1f}" y1="{ly:.1f}" x2="{width - pad_r + 6:.1f}" y2="{ly:.1f}" stroke="{last_col}" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{last_col}" stroke="#111722" stroke-width="2"/>'
        f'<text x="{width - pad_r + 10:.1f}" y="{ly - 6:.1f}" text-anchor="end" font-size="12" font-weight="700" fill="{C_INK}" style="font-variant-numeric:tabular-nums">${last_v:,.0f}</text>'
    )

    # Crosshair + tooltip interaction layer (data embedded for the inline script).
    pt_json = json.dumps([[round(px, 1), round(py, 1)] for px, py in pts])
    meta_json = json.dumps([[history[i]["time"], values[i]] for i in range(n)])
    gid = "eq"

    return f'''<svg id="{gid}-svg" viewBox="0 0 {width} {height}" width="100%" style="height:{height}px;display:block" role="img" aria-label="Stock portfolio equity curve"
      data-pts='{pt_json}' data-meta='{meta_json}' data-baseline="{baseline}">
      <defs>
        <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{C_SERIES}" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="{C_SERIES}" stop-opacity="0.01"/>
        </linearGradient>
      </defs>
      {''.join(grid)}
      <line x1="{pad_l}" y1="{baseline_y:.1f}" x2="{width - pad_r}" y2="{baseline_y:.1f}" stroke="{C_AXIS}" stroke-width="1" stroke-dasharray="5,4"/>
      <text x="{pad_l + 4}" y="{baseline_y - 6:.1f}" text-anchor="start" font-size="10" fill="{C_MUTED}">${baseline:,.0f} baseline</text>
      <path d="{area_d}" fill="url(#eqFill)" stroke="none"/>
      <path d="{path_d}" fill="none" stroke="{C_SERIES}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      {last_label}
      {''.join(x_labels)}
      <g id="{gid}-hover" style="display:none">
        <line id="{gid}-vline" y1="{pad_t}" y2="{pad_t + plot_h:.1f}" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3,3"/>
        <circle id="{gid}-dot" r="4.5" fill="{C_SERIES}" stroke="#0d1420" stroke-width="2"/>
      </g>
      <rect id="{gid}-hit" x="{pad_l}" y="{pad_t}" width="{plot_w:.1f}" height="{plot_h:.1f}" fill="transparent" style="cursor:crosshair"/>
      <g id="{gid}-tip" style="display:none" pointer-events="none">
        <rect id="{gid}-tip-bg" rx="6" ry="6" fill="#0c1017" stroke="rgba(255,255,255,0.12)"/>
        <text id="{gid}-tip-d" font-size="11" fill="{C_MUTED}"></text>
        <text id="{gid}-tip-v" font-size="13" font-weight="700" fill="{C_INK}" style="font-variant-numeric:tabular-nums"></text>
      </g>
    </svg>
    <script>(function(){{
      var svg=document.getElementById("{gid}-svg"); if(!svg) return;
      var pts=JSON.parse(svg.getAttribute("data-pts")), meta=JSON.parse(svg.getAttribute("data-meta"));
      var base=parseFloat(svg.getAttribute("data-baseline"));
      var hit=document.getElementById("{gid}-hit"), hov=document.getElementById("{gid}-hover");
      var vline=document.getElementById("{gid}-vline"), dot=document.getElementById("{gid}-dot");
      var tip=document.getElementById("{gid}-tip"), bg=document.getElementById("{gid}-tip-bg");
      var td=document.getElementById("{gid}-tip-d"), tv=document.getElementById("{gid}-tip-v");
      var VB={width}, PADT={pad_t};
      function locate(ev){{
        var r=svg.getBoundingClientRect(), sx=(ev.clientX-r.left)*(VB/r.width);
        var best=0,bd=1e9; for(var i=0;i<pts.length;i++){{var d=Math.abs(pts[i][0]-sx); if(d<bd){{bd=d;best=i;}}}}
        return best;
      }}
      function fmt(v){{return "$"+v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}});}}
      function show(ev){{
        var i=locate(ev), p=pts[i], m=meta[i];
        hov.style.display=""; tip.style.display="";
        vline.setAttribute("x1",p[0]); vline.setAttribute("x2",p[0]);
        dot.setAttribute("cx",p[0]); dot.setAttribute("cy",p[1]);
        var up=m[1]>=base; dot.setAttribute("fill", up?"#22c55e":"#ef4444");
        td.textContent=m[0]; tv.textContent=fmt(m[1]);
        var dl=(m[0]+"").length*6.2+16, vl=fmt(m[1]).length*7.4+16, w=Math.max(dl,vl,86), h=40;
        var tx=p[0]+12; if(tx+w>VB-6) tx=p[0]-w-12; var ty=Math.max(PADT, p[1]-h-10);
        bg.setAttribute("x",tx); bg.setAttribute("y",ty); bg.setAttribute("width",w); bg.setAttribute("height",h);
        td.setAttribute("x",tx+9); td.setAttribute("y",ty+16);
        tv.setAttribute("x",tx+9); tv.setAttribute("y",ty+32);
      }}
      hit.addEventListener("mousemove",show);
      hit.addEventListener("mouseleave",function(){{hov.style.display="none"; tip.style.display="none";}});
    }})();</script>'''


def _bar_row(label: str, value: float, max_abs: float, fmt: str = "${:+,.2f}", title: str | None = None) -> str:
    """One CSS horizontal bar (magnitude relative to max_abs, colored by sign)."""
    max_abs = max_abs or 1
    pct = min(abs(value) / max_abs * 100, 100)
    css = "pos" if value >= 0 else "neg"
    return f'''<div class="bar-row" title="{title or label}">
        <span class="bar-label">{label}</span>
        <div class="bar-track"><div class="bar-fill {css}" style="width:{pct:.1f}%"></div></div>
        <span class="bar-value {css}">{fmt.format(value)}</span>
    </div>'''


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _build_api_health_html(api_health: list[dict] | None) -> str:
    if not api_health:
        return ""

    rows = ""
    for a in api_health:
        status = a.get("status", "unknown")
        css = {"ok": "pos", "degraded": "yellow", "error": "neg", "no_key": "dim"}.get(status, "dim")
        icon = {"ok": "OK", "degraded": "SLOW", "error": "DOWN", "no_key": "N/A"}.get(status, "?")
        latency = f'{a["latency_ms"]}ms' if "latency_ms" in a else "-"
        fallback = a.get("fallback", "")
        rows += f"""<tr>
            <td><b>{a['name']}</b></td>
            <td>{a.get('bot', '')}</td>
            <td class="{css}"><b>{icon}</b></td>
            <td>{latency}</td>
            <td style="color:#666;font-size:11px">{a.get('message', '')}</td>
            <td style="color:#555;font-size:10px">{fallback[:60]}</td>
        </tr>"""

    ok = sum(1 for a in api_health if a.get("status") == "ok")
    total = len(api_health)

    return f"""
    <div class="bot-card" style="grid-column: 1 / -1; margin-top: 8px;">
        <div class="bot-header">
            <div class="bot-title">API STATUS <span class="bot-tag">{ok}/{total} OK</span></div>
        </div>
        <div class="table-scroll"><table>
            <tr><th>Service</th><th>Bot</th><th>Status</th><th>Latency</th><th>Details</th><th>Fallback</th></tr>
            {rows}
        </table></div>
    </div>"""


def _build_risk_html(fx: dict, stock: dict | None, state: dict) -> str:
    cbr = state.get("cross_bot_risk", {}) or {}
    flags = cbr.get("flags", []) or []
    overlaps = cbr.get("overlaps", {}) or {}
    by_bot = cbr.get("by_bot", {}) or {}

    # Currency exposure: notional per base currency across open FX positions, signed by side.
    exposure: dict[str, float] = {}
    for t in fx["open_trades"]:
        try:
            notional = float(t.get("entry_price") or 0) * float(t.get("quantity") or 0)
        except Exception:
            notional = 0.0
        sign = 1 if str(t.get("side", "")).lower() in ("long", "buy") else -1
        base = (t.get("symbol") or "???")[:3]
        exposure[base] = exposure.get(base, 0.0) + sign * notional
    max_exp = max((abs(v) for v in exposure.values()), default=0)
    exposure_html = "".join(
        _bar_row(ccy, v, max_exp, fmt="${:+,.0f}") for ccy, v in sorted(exposure.items(), key=lambda kv: -abs(kv[1]))
    ) or '<div class="empty">No open FX exposure</div>'

    # Overlaps
    if overlaps:
        overlap_html = "".join(
            f'<div class="bar-row"><span class="bar-label">{sym}</span><span style="color:#f59e0b;font-size:12px">{", ".join(bots)}</span></div>'
            for sym, bots in overlaps.items()
        )
    else:
        overlap_html = '<div class="empty">No overlapping positions detected</div>'

    # Drawdown (from stock equity curve — the only true long-run history we have)
    dd_html = '<div class="empty">Not enough history</div>'
    if stock and stock.get("equity_history"):
        vals = [h["value"] for h in stock["equity_history"]]
        peak = vals[0]
        peak_i = 0
        max_dd = 0.0
        max_dd_i = 0
        for i, v in enumerate(vals):
            if v > peak:
                peak, peak_i = v, i
            dd = (peak - v) / peak * 100 if peak else 0
            if dd > max_dd:
                max_dd, max_dd_i = dd, i
        current_dd = (peak - vals[-1]) / peak * 100 if peak else 0
        dd_css = "neg" if current_dd > 0.5 else "pos"
        dd_html = f'''
        <div class="mini-stat"><span class="label">Current Drawdown</span><span class="val {dd_css}">-{current_dd:.2f}%</span></div>
        <div class="mini-stat"><span class="label">Max Drawdown (stock)</span><span class="val neg">-{max_dd:.2f}%</span></div>
        <div class="mini-stat"><span class="label">Peak Equity</span><span class="val">${peak:,.0f}</span></div>'''

    flags_html = "".join(f'<span class="flag-badge">{f}</span>' for f in flags) or '<span style="color:#555;font-size:12px">None</span>'

    total_exposure = cbr.get("total_exposure")
    total_positions = cbr.get("total_positions")

    return f"""
    <div class="risk-grid">
        <div class="risk-card">
            <h3>Currency Exposure (FX)</h3>
            {exposure_html}
        </div>
        <div class="risk-card">
            <h3>Cross-Bot Overlaps</h3>
            {overlap_html}
            <h3 style="margin-top:16px">Risk Flags</h3>
            {flags_html}
        </div>
        <div class="risk-card">
            <h3>Total Cross-Bot Exposure</h3>
            <div class="bot-stats" style="grid-template-columns: 1fr 1fr;">
                <div class="mini-stat"><span class="label">Total Exposure</span><span class="val">{f'${total_exposure:,.0f}' if total_exposure is not None else 'N/A'}</span></div>
                <div class="mini-stat"><span class="label">Total Positions</span><span class="val">{total_positions if total_positions is not None else 'N/A'}</span></div>
                {''.join(f'<div class="mini-stat"><span class="label">{b.upper()}</span><span class="val">{v.get("count", 0)} pos / ${v.get("total_value", 0):,.0f}</span></div>' for b, v in by_bot.items())}
            </div>
        </div>
        <div class="risk-card">
            <h3>Drawdown Status</h3>
            <div class="bot-stats" style="grid-template-columns: 1fr;">
                {dd_html}
            </div>
        </div>
    </div>"""


_INDEX_ACCENT = {200: "#3987e5", 201: "#eda100", 202: "#e8b923"}  # S&P blue, Nasdaq amber, Gold


def _signal_badge(action: str) -> str:
    a = (action or "flat").lower()
    label = a.replace("_", " ").upper()
    cls = {
        "enter_long": "sig-enter", "entry": "sig-enter",
        "hold_long": "sig-hold", "hold": "sig-hold",
        "exit_long": "sig-exit", "exit": "sig-exit",
        "flat": "sig-flat",
    }.get(a, "sig-flat")
    return f'<span class="sig-badge {cls}">{label}</span>'


def _short_ts(ts: str) -> str:
    if not ts:
        return "—"
    s = str(ts).replace("T", " ").replace("Z", "")
    return s[:16]


def _build_index_html(index: dict | None) -> str:
    """Index Trend (dry-run) panel — S&P / Nasdaq / Gold. Paper only, no orders."""
    if not index or not index.get("strategies"):
        return ""

    cards = ""
    for s in index["strategies"]:
        st = s.get("state") or {}
        sid = s["strategy_id"]
        accent = _INDEX_ACCENT.get(sid, "#3987e5")
        close = st.get("close", s.get("price"))
        sma = st.get("sma_200")
        above = st.get("above_sma")
        dist = st.get("dist_to_sma_pct")
        if dist is None and close and sma:
            try:
                dist = (float(close) - float(sma)) / float(sma) * 100
            except Exception:
                dist = None
        hist = st.get("macd_histogram")
        macd_bull = st.get("macd_bullish")
        if macd_bull is None and hist is not None:
            macd_bull = hist > 0

        dist_css = "pos" if above else "neg"
        dist_txt = f"{dist:+.2f}%" if isinstance(dist, (int, float)) else "—"
        sma_txt = f"${sma:,.2f}" if isinstance(sma, (int, float)) else "—"
        macd_css = "pos" if macd_bull else "neg"
        macd_txt = "BULLISH &gt;0" if macd_bull else "bearish &le;0"
        hist_txt = f"{hist:+.3f}" if isinstance(hist, (int, float)) else "—"
        close_txt = f"${close:,.2f}" if isinstance(close, (int, float)) else "—"

        adx_row = ""
        if st.get("use_adx"):
            adx = st.get("adx_14")
            adx_pass = st.get("adx_pass")
            adx_min = st.get("adx_min", 25)
            gate_css = "pos" if adx_pass else "neg"
            gate_txt = "PASS" if adx_pass else "BLOCK"
            adx_val = f"{adx:.2f}" if isinstance(adx, (int, float)) else "—"
            adx_row = (
                f'<div class="ix-metric"><span class="ix-k">ADX-14 gate</span>'
                f'<span class="ix-v"><b class="{gate_css}">{adx_val}</b> '
                f'<span class="ix-gate {gate_css}">{gate_txt}</span> '
                f'<span class="dim">&gt;{adx_min:g}</span></span></div>'
            )

        cards += f'''
        <div class="ix-card" style="--accent:{accent}">
            <div class="ix-top">
                <div class="ix-name">{s['name']}<span class="ix-sym">{s['symbol']}</span></div>
                {_signal_badge(s.get('action'))}
            </div>
            <div class="ix-price">{close_txt}<span class="ix-date">{st.get('date') or _short_ts(s.get('generated_at'))}</span></div>
            <div class="ix-metrics">
                <div class="ix-metric"><span class="ix-k">vs SMA-200</span><span class="ix-v"><b class="{dist_css}">{dist_txt}</b> <span class="dim">{sma_txt}</span></span></div>
                <div class="ix-metric"><span class="ix-k">MACD hist</span><span class="ix-v"><b class="{macd_css}">{hist_txt}</b> <span class="{macd_css}">{macd_txt}</span></span></div>
                {adx_row}
            </div>
        </div>'''

    # Open dry-run intents
    intents = index.get("intents", [])
    if intents:
        name_by_sid = {m["strategy_id"]: m["name"] for m in INDEX_STRATEGIES}
        rows = ""
        for it in intents:
            entry = it.get("entry_price")
            qty = it.get("quantity")
            stop = it.get("stop_loss")
            rows += (
                f'<tr><td><b>{it.get("symbol")}</b> <span class="dim">{name_by_sid.get(it.get("strategy_id"), "")}</span></td>'
                f'<td>{str(it.get("side","long")).upper()}</td>'
                f'<td>{f"${entry:,.2f}" if isinstance(entry,(int,float)) else "—"}</td>'
                f'<td>{f"{qty:,.4f}" if isinstance(qty,(int,float)) else "—"}</td>'
                f'<td>{f"${stop:,.2f}" if isinstance(stop,(int,float)) else "—"}</td>'
                f'<td class="dim" style="font-size:11px">{_short_ts(it.get("created_at"))}</td></tr>'
            )
        intents_html = (
            '<h3>Open Dry-Run Intents <span class="ix-paper">status=intent · no order</span></h3>'
            '<div class="table-scroll"><table>'
            '<tr><th>Instrument</th><th>Side</th><th>Entry</th><th>Qty (pts)</th><th>Stop</th><th>Logged</th></tr>'
            f'{rows}</table></div>'
        )
    else:
        intents_html = '<h3>Open Dry-Run Intents</h3><div class="empty">No open intent positions</div>'

    # Recent signal log
    recent = index.get("recent", [])
    if recent:
        rrows = ""
        for r in recent:
            price = r.get("price")
            rrows += (
                f'<tr><td class="dim" style="font-size:11px">{_short_ts(r.get("generated_at"))}</td>'
                f'<td><b>{r.get("symbol")}</b></td>'
                f'<td>{_signal_badge(r.get("action"))}</td>'
                f'<td style="font-variant-numeric:tabular-nums">{f"${price:,.2f}" if isinstance(price,(int,float)) else "—"}</td></tr>'
            )
        recent_html = (
            '<h3>Recent Signal Log</h3>'
            '<div class="table-scroll"><table>'
            '<tr><th>When</th><th>Symbol</th><th>Signal</th><th>Price</th></tr>'
            f'{rrows}</table></div>'
        )
    else:
        recent_html = ""

    return f'''
    <section class="panel index-panel">
        <div class="panel-head">
            <h2>Index Trend <span class="panel-sub">SMA-200 + MACD trend · long-only</span></h2>
            <span class="dryrun-badge">DRY-RUN · PAPER · NO ORDERS</span>
        </div>
        <div class="ix-cards">{cards}</div>
        {intents_html}
        {recent_html}
    </section>'''


def build_dashboard(fx: dict, stock: dict | None, poly: dict | None,
                    api_health: list[dict] | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = get_global_state()
    index = get_index_data()
    balance = get_total_balance(fx, stock)

    # Aggregate totals
    bots_online = 1 if (fx.get("account") or fx.get("open_trades")) else 0
    fx_total_pnl = fx.get("unrealized_pnl", 0) + fx.get("realized_pnl", 0)
    total_pnl = fx_total_pnl
    total_open = fx["open_count"]

    stock_pnl = 0
    if stock:
        bots_online += 1
        stock_pnl = stock.get("total_pnl", 0)
        total_pnl += stock_pnl
        total_open += len(stock.get("open_positions", []))

    poly_pnl = 0
    if poly:
        bots_online += 1
        poly_pnl = poly["ev"]["pnl"] + poly["weather"]["pnl"]
        total_pnl += poly_pnl
        total_open += poly["ev"]["open"] + poly["weather"]["open"]

    regime = fx.get("regime") or {}
    regime_label = regime.get("regime") or "N/A"
    regime_desc = regime.get("description", "")
    vix = regime.get("vix")
    vix_label = "N/A"
    if isinstance(vix, (int, float)):
        vix_label = f"{vix:.1f}"
        vix_desc = "Low" if vix < 15 else "Normal" if vix < 20 else "Elevated" if vix < 30 else "High"
    else:
        vix_desc = ""

    us_open = _us_market_open()

    equity_svg = render_equity_svg(stock.get("equity_history", []) if stock else [])

    # FX position rows
    fx_rows = ""
    for t in fx["open_trades"]:
        held = f"{t['days_held']}d" if t.get("days_held") is not None else "?"
        swap = f"${t['swap_est']:+.2f}" if t.get("swap_est") is not None else "—"
        upnl = t.get("unrealized_pnl")
        if upnl is None:
            upnl_html = '<span class="dim">n/a (offline)</span>'
        else:
            upnl_css = "pos" if upnl >= 0 else "neg"
            entry_val = t.get("entry_price", 0) * t.get("quantity", 0)
            upnl_pct = (upnl / entry_val * 100) if entry_val else 0
            upnl_html = f'<span class="{upnl_css}">${upnl:+,.2f} ({upnl_pct:+.1f}%)</span>'
        fx_rows += f"""<tr>
            <td><b>{t['symbol']}</b></td>
            <td>{str(t['side']).upper()}</td>
            <td>{t.get('quantity', '?')}</td>
            <td>{t['entry_price']}</td>
            <td>{upnl_html}</td>
            <td>{held}</td>
            <td>{swap}</td>
        </tr>"""
    if not fx_rows:
        fx_rows = '<tr><td colspan="7" class="empty">No open positions</td></tr>'

    fx_live_tag = '<span class="market-status live-tag">LIVE</span>' if fx.get("live") else '<span class="market-status offline-tag">LOCAL SNAPSHOT</span>'

    fx_html = f"""
    <div class="bot-card">
        <div class="bot-header">
            <div class="bot-title">FX BOT <span class="bot-tag">Capital.com</span> {fx_live_tag} <span class="market-status {'market-open' if fx.get('market_open') else 'market-closed'}">{'OPEN' if fx.get('market_open') else 'CLOSED'}</span></div>
            <div class="bot-pnl {'pos' if fx_total_pnl >= 0 else 'neg'}">${fx_total_pnl:+,.2f}</div>
        </div>
        <div class="bot-stats">
            <div class="mini-stat"><span class="label">Equity</span><span class="val">${fx.get('account', {}).get('equity', 0):,.2f}</span></div>
            <div class="mini-stat"><span class="label">Unrealized</span><span class="val {'pos' if fx['unrealized_pnl'] >= 0 else 'neg'}">${fx['unrealized_pnl']:+,.2f}</span></div>
            <div class="mini-stat"><span class="label">Realized</span><span class="val {'pos' if fx['realized_pnl'] >= 0 else 'neg'}">${fx['realized_pnl']:+,.2f}</span></div>
            <div class="mini-stat"><span class="label">Open</span><span class="val">{fx['open_count']}</span></div>
            <div class="mini-stat"><span class="label">Closed (win%)</span><span class="val">{fx['closed_count']} ({fx['win_rate']}%)</span></div>
            <div class="mini-stat"><span class="label">Est. Swap Cost</span><span class="val neg">${fx['total_swap_est']:+,.2f}</span></div>
        </div>
        <h3>Open Positions</h3>
        <div class="table-scroll"><table>
            <tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>P&L</th><th>Held</th><th>Swap Est.</th></tr>
            {fx_rows}
        </table></div>
    </div>"""

    # Stock section
    stock_html = ""
    if stock:
        pv = stock["portfolio_value"]
        pnl = stock["total_pnl"]
        pnl_pct = (pnl / 100000) * 100
        positions = stock.get("open_positions", [])
        pos_html = ""
        for p in positions:
            unrealized = p.get("unrealized_pnl", 0)
            entry_cost = p.get("entry", 0) * p.get("shares", 0)
            u_pct = (unrealized / entry_cost * 100) if entry_cost else 0
            pos_html += f"""<tr>
                <td><b>{p['symbol']}</b></td>
                <td>{p.get('side', 'BUY')}</td>
                <td>{p['shares']}</td>
                <td>${p['entry']}</td>
                <td class="{'pos' if unrealized >= 0 else 'neg'}">${unrealized:+,.2f} ({u_pct:+.1f}%)</td>
            </tr>"""
        if not pos_html:
            pos_html = '<tr><td colspan="5" class="empty">No open positions</td></tr>'

        closed_html = ""
        for t in stock.get("closed_trades", [])[:5]:
            rpnl = t.get("realized_pnl", 0)
            entry_cost = t.get("entry", 0) * t.get("shares", 1)
            r_pct = (rpnl / entry_cost * 100) if entry_cost else 0
            closed_html += f"""<tr>
                <td>{t.get('symbol','?')}</td>
                <td>${t.get('entry','?')}</td>
                <td>${t.get('exit','?')}</td>
                <td class="{'pos' if rpnl >= 0 else 'neg'}">${rpnl:+,.2f} ({r_pct:+.1f}%)</td>
            </tr>"""

        # Top winners/losers (from closed trades, else fall back to open unrealized P&L)
        source_trades = stock.get("closed_trades") or positions
        pnl_key = "realized_pnl" if stock.get("closed_trades") else "unrealized_pnl"
        ranked = sorted(source_trades, key=lambda t: t.get(pnl_key, 0), reverse=True)
        winners = [t for t in ranked if t.get(pnl_key, 0) > 0][:3]
        losers = [t for t in ranked if t.get(pnl_key, 0) < 0][-3:][::-1]
        max_abs_wl = max([abs(t.get(pnl_key, 0)) for t in (winners + losers)], default=1)
        wl_html = "".join(_bar_row(t.get("symbol", "?"), t.get(pnl_key, 0), max_abs_wl) for t in (winners + losers))
        if not wl_html:
            wl_html = '<div class="empty">No trades yet</div>'

        stock_html = f"""
        <div class="bot-card">
            <div class="bot-header">
                <div class="bot-title">STOCK/ETF BOT <span class="bot-tag">Alpaca</span> <span class="market-status {'market-open' if us_open else 'market-closed'}">{'OPEN' if us_open else 'CLOSED'}</span></div>
                <div class="bot-pnl {'pos' if pnl >= 0 else 'neg'}">${pnl:+,.2f} ({pnl_pct:+.2f}%)</div>
            </div>
            <div class="bot-stats">
                <div class="mini-stat"><span class="label">Portfolio</span><span class="val">${pv:,.2f}</span></div>
                <div class="mini-stat"><span class="label">Cash</span><span class="val">${stock.get('cash', 0):,.2f}</span></div>
                <div class="mini-stat"><span class="label">Positions</span><span class="val">{len(positions)}</span></div>
                <div class="mini-stat"><span class="label">Last Run</span><span class="val">{stock.get('last_run') or 'N/A'}</span></div>
            </div>
            <div class="table-scroll"><table>
                <tr><th>Symbol</th><th>Side</th><th>Shares</th><th>Entry</th><th>Unrealized</th></tr>
                {pos_html}
            </table></div>
            {f'<h3>Closed Trades</h3><div class="table-scroll"><table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th></tr>{closed_html}</table></div>' if closed_html else ''}
            <h3>Top Winners / Losers</h3>
            {wl_html}
        </div>"""

    # Polymarket / Weather section
    poly_html = ""
    if poly:
        ev = poly["ev"]
        w = poly["weather"]
        we = poly.get("weather_extra", {"avg_win": 0, "avg_loss": 0, "yes": {}, "no": {}})
        total_poly_pnl = ev["pnl"] + w["pnl"]
        total_risked = ev["risked"] + w["risked"]

        recent_html = ""
        for t in (poly.get("weather_recent", []) + poly.get("ev_recent", []))[-6:]:
            q = (t.get("market") or t.get("question", "?"))[:40]
            size = float(t.get("size_usd", 0) or 0)
            st = "WON" if t.get("won") else "LOST" if t.get("resolved") else "OPEN"
            rpnl = float(t.get("realized_pnl", 0) or 0)
            rpnl_pct = (rpnl / size * 100) if size else 0
            css = "pos" if rpnl > 0 else "neg" if rpnl < 0 else ""
            recent_html += f'<tr><td>{q}</td><td>${size:.2f}</td><td>{st}</td><td class="{css}">${rpnl:+.2f} ({rpnl_pct:+.0f}%)</td></tr>'

        yes_s, no_s = we.get("yes", {}), we.get("no", {})
        max_side = max(abs(yes_s.get("pnl", 0)), abs(no_s.get("pnl", 0))) or 1
        side_html = (
            _bar_row(f"YES ({yes_s.get('win_rate', 0)}% win)", yes_s.get("pnl", 0), max_side)
            + _bar_row(f"NO ({no_s.get('win_rate', 0)}% win)", no_s.get("pnl", 0), max_side)
        )

        poly_html = f"""
        <div class="bot-card">
            <div class="bot-header">
                <div class="bot-title">POLYMARKET BOT <span class="bot-tag">Paper</span> <span class="market-status market-247">24/7</span></div>
                <div class="bot-pnl {'pos' if total_poly_pnl >= 0 else 'neg'}">${total_poly_pnl:+,.2f} ({total_poly_pnl / total_risked * 100 if total_risked else 0:+.1f}%)</div>
            </div>
            <div class="bot-stats">
                <div class="mini-stat"><span class="label">EV Trades</span><span class="val">{ev['total']} ({ev['open']} open)</span></div>
                <div class="mini-stat"><span class="label">EV Win Rate</span><span class="val">{ev['win_rate']}%</span></div>
                <div class="mini-stat"><span class="label">EV P&L</span><span class="val">${ev['pnl']:+.2f}</span></div>
                <div class="mini-stat"><span class="label">Weather Trades</span><span class="val">{w['total']} ({w['open']} open)</span></div>
                <div class="mini-stat"><span class="label">Weather Win Rate</span><span class="val">{w['win_rate']}%</span></div>
                <div class="mini-stat"><span class="label">Weather P&L</span><span class="val">${w['pnl']:+.2f}</span></div>
                <div class="mini-stat"><span class="label">Weather Avg Win</span><span class="val pos">${we.get('avg_win', 0):+.2f}</span></div>
                <div class="mini-stat"><span class="label">Weather Avg Loss</span><span class="val neg">${we.get('avg_loss', 0):+.2f}</span></div>
            </div>
            <h3>Weather P&L by Side</h3>
            {side_html}
            <h3>Recent Trades</h3>
            <div class="table-scroll"><table>
                <tr><th>Market</th><th>Size</th><th>Status</th><th>P&L</th></tr>
                {recent_html}
            </table></div>
        </div>"""

    risk_html = _build_risk_html(fx, stock, state)

    index_html = _build_index_html(index)

    # ---- Total Balance hero (headline number of the whole page) ----
    if balance["delta_abs"] is not None:
        d_abs, d_pct = balance["delta_abs"], balance["delta_pct"]
        d_css = "pos" if d_abs >= 0 else "neg"
        d_arrow = "&#9650;" if d_abs >= 0 else "&#9660;"
        pct_txt = f" ({d_pct:+.2f}%)" if d_pct is not None else ""
        delta_html = f'<div class="hero-delta {d_css}">{d_arrow} ${abs(d_abs):,.2f}{pct_txt} <span class="hero-delta-sub">vs prev day</span></div>'
    else:
        delta_html = '<div class="hero-delta dim">single day of history &middot; no change yet</div>'

    fx_note = "live" if balance["fx_live"] else "base + realized"
    hero_html = f"""
    <div class="hero">
        <div class="hero-main">
            <div class="hero-label">Total Balance <span>&middot; all bots combined</span></div>
            <div class="hero-value num">${balance['total']:,.2f}</div>
            {delta_html}
        </div>
        <div class="hero-breakdown">
            <div class="hb"><span class="hb-dot" style="background:var(--pos)"></span><span class="hb-k">Stock</span><span class="hb-v num">${balance['stock']:,.2f}</span></div>
            <div class="hb"><span class="hb-dot" style="background:var(--accent)"></span><span class="hb-k">FX <span class="hb-note">{fx_note}</span></span><span class="hb-v num">${balance['fx']:,.2f}</span></div>
            <div class="hb"><span class="hb-dot" style="background:var(--violet)"></span><span class="hb-k">Poly <span class="hb-note">{balance['poly_books']} books</span></span><span class="hb-v num">${balance['poly']:,.2f}</span></div>
        </div>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Admin Dashboard</title>
<style>
:root {{
    --bg: #0a0d13;
    --panel: #12161f;
    --tile: #171c27;
    --tile-2: #0f131b;
    --border: rgba(255,255,255,0.07);
    --border-2: rgba(255,255,255,0.12);
    --ink: #e9ecf2;
    --ink-2: #9aa3b2;
    --muted: #626b7a;
    --accent: #3987e5;
    --pos: #22c55e;
    --neg: #ef4444;
    --warn: #fab219;
    --violet: #9085e9;
    --amber: #eda100;
    --radius: 14px;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.18);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
    font-family: system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background:
        radial-gradient(1200px 600px at 15% -10%, rgba(57,135,229,0.08), transparent 60%),
        radial-gradient(1000px 500px at 100% 0%, rgba(144,133,233,0.06), transparent 55%),
        var(--bg);
    color: var(--ink);
    padding: 22px;
    max-width: 1440px;
    margin: 0 auto;
    line-height: 1.45;
    overflow-x: hidden;
}}
.num {{ font-variant-numeric: tabular-nums; }}

/* Header */
.header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 14px;
    margin-bottom: 22px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}}
.header .brand {{ display: flex; align-items: center; gap: 12px; }}
.header .dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--pos); box-shadow: 0 0 0 4px rgba(34,197,94,0.15); }}
.header h1 {{ font-size: 22px; font-weight: 800; letter-spacing: -0.02em; }}
.header h1 span {{ color: var(--accent); }}
.chips {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.chip {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--panel); border: 1px solid var(--border);
    padding: 6px 11px; border-radius: 999px; font-size: 12px; color: var(--ink-2);
}}
.chip b {{ color: var(--ink); font-weight: 700; }}

/* Total Balance hero */
.hero {{
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 22px;
    background:
        linear-gradient(180deg, rgba(57,135,229,0.10), rgba(57,135,229,0.02)),
        var(--panel);
    border: 1px solid var(--border-2);
    border-radius: 18px;
    box-shadow: var(--shadow);
    padding: 22px 26px;
    margin-bottom: 20px;
}}
.hero-main {{ min-width: 240px; }}
.hero-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1.2px; color: var(--ink-2); font-weight: 700; }}
.hero-label span {{ color: var(--muted); font-weight: 500; letter-spacing: 0.4px; }}
.hero-value {{ font-size: 46px; font-weight: 800; letter-spacing: -0.03em; line-height: 1.05; margin: 6px 0 8px; }}
.hero-delta {{ font-size: 14px; font-weight: 700; }}
.hero-delta-sub {{ color: var(--muted); font-weight: 500; font-size: 12px; margin-left: 4px; }}
.hero-breakdown {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.hb {{
    display: flex; align-items: center; gap: 8px;
    background: var(--tile); border: 1px solid var(--border);
    border-radius: 11px; padding: 10px 14px; min-width: 150px;
}}
.hb-dot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }}
.hb-k {{ font-size: 12px; color: var(--ink-2); font-weight: 600; }}
.hb-note {{ font-size: 10px; color: var(--muted); font-weight: 500; }}
.hb-v {{ font-size: 16px; font-weight: 800; margin-left: auto; padding-left: 10px; }}

/* Overview KPI tiles */
.overview {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 20px;
}}
.overview-card {{
    position: relative;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow);
    overflow: hidden;
}}
.overview-card::before {{
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: var(--accent); opacity: 0.9;
}}
.overview-card.k-pnl::before {{ background: var(--pos); }}
.overview-card.k-neg::before {{ background: var(--neg); }}
.overview-card.k-pos::before {{ background: var(--pos); }}
.overview-card.k-open::before {{ background: var(--accent); }}
.overview-card.k-regime::before {{ background: var(--violet); }}
.overview-card.k-vix::before {{ background: var(--warn); }}
.overview-card .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.7px; font-weight: 600; }}
.overview-card .value {{ font-size: 30px; font-weight: 800; margin-top: 6px; letter-spacing: -0.02em; }}
.overview-card .sub {{ font-size: 12px; color: var(--ink-2); margin-top: 5px; }}

/* Colors */
.pos {{ color: var(--pos); }}
.neg {{ color: var(--neg); }}
.green {{ color: var(--pos); }}
.blue {{ color: var(--accent); }}
.yellow {{ color: var(--warn); }}
.red {{ color: var(--neg); }}
.dim {{ color: var(--muted); }}

/* Regime */
.regime-trending {{ color: var(--pos); }}
.regime-ranging {{ color: var(--warn); }}
.regime-volatile {{ color: var(--neg); }}
.regime-crisis {{ color: var(--neg); font-weight: bold; }}
.regime-n\\/a {{ color: var(--muted); }}

/* Panels (shared card shell) */
.panel {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 18px;
    margin-bottom: 20px;
}}
.panel-head {{
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 10px; margin-bottom: 16px;
}}
.panel-head h2 {{ font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }}
.panel-sub {{ font-size: 12px; color: var(--muted); font-weight: 500; margin-left: 8px; }}

/* Chart */
.chart-container {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 20px;
    overflow: hidden;
}}
.chart-header {{
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 8px;
    padding: 14px 18px; border-bottom: 1px solid var(--border);
}}
.chart-title {{ font-size: 15px; font-weight: 700; }}
.chart-legend {{ display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2); }}
.swatch {{ width: 12px; height: 3px; border-radius: 2px; background: var(--accent); display: inline-block; }}
#chart {{ padding: 12px 10px 6px; overflow-x: auto; }}

/* Bot cards grid */
.bots {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px; }}
.bot-card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 16px 18px;
    overflow: hidden;
    min-width: 0;
}}
.bot-header {{
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 14px; padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap; gap: 6px;
}}
.bot-title {{ font-size: 13px; font-weight: 700; letter-spacing: 0.02em; }}
.bot-tag {{
    background: rgba(255,255,255,0.05); color: var(--ink-2);
    padding: 2px 8px; border-radius: 5px; font-size: 10px; font-weight: 600;
    margin-left: 6px; border: 1px solid var(--border);
}}
.bot-pnl {{ font-size: 19px; font-weight: 800; }}
.market-status {{
    display: inline-block; padding: 2px 8px; border-radius: 5px;
    font-size: 10px; font-weight: 700; margin-left: 6px;
}}
.market-open {{ background: rgba(34,197,94,0.14); color: var(--pos); }}
.market-closed {{ background: rgba(239,68,68,0.13); color: var(--neg); }}
.market-247 {{ background: rgba(144,133,233,0.15); color: var(--violet); }}
.live-tag {{ background: rgba(34,197,94,0.14); color: var(--pos); }}
.offline-tag {{ background: rgba(250,178,25,0.14); color: var(--warn); }}
.bot-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px; }}
.mini-stat {{ background: var(--tile-2); border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; }}
.mini-stat .label {{ display: block; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }}
.mini-stat .val {{ display: block; font-size: 13px; font-weight: 700; margin-top: 3px; font-variant-numeric: tabular-nums; }}

h3 {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--muted); font-weight: 700; margin: 16px 0 8px;
}}

/* Tables */
.table-scroll {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.table-scroll table {{ min-width: 340px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{
    text-align: left; padding: 6px 8px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.4px; color: var(--muted); font-weight: 600;
    border-bottom: 1px solid var(--border); white-space: nowrap;
}}
td {{ padding: 7px 8px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.04); white-space: nowrap; }}
tbody tr:last-child td, table tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: rgba(255,255,255,0.03); }}
.empty {{ text-align: center; color: var(--muted); padding: 18px !important; }}

.badge {{ display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 10px; font-weight: 700; }}
.badge-t {{ background: rgba(34,197,94,0.14); color: var(--pos); }}
.badge-pa {{ background: rgba(144,133,233,0.15); color: var(--violet); }}
.flag-badge {{
    display: inline-block; background: rgba(239,68,68,0.13); color: var(--neg);
    padding: 3px 10px; border-radius: 5px; font-size: 10px; font-weight: 700; margin: 2px 4px 2px 0;
}}
.sig-entry {{ color: var(--pos); font-weight: bold; }}
.sig-exit {{ color: var(--neg); font-weight: bold; }}

/* Index Trend panel */
.dryrun-badge {{
    font-size: 10px; font-weight: 800; letter-spacing: 0.6px; color: var(--warn);
    background: rgba(250,178,25,0.10); border: 1px solid rgba(250,178,25,0.32);
    padding: 5px 11px; border-radius: 999px;
}}
.ix-cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 8px; }}
.ix-card {{
    background: var(--tile); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; border-top: 3px solid var(--accent); min-width: 0;
}}
.ix-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.ix-name {{ font-weight: 700; font-size: 14px; }}
.ix-sym {{
    margin-left: 8px; font-size: 10px; color: var(--ink-2); background: rgba(255,255,255,0.05);
    padding: 2px 7px; border-radius: 5px; font-weight: 600; letter-spacing: 0.5px;
}}
.ix-price {{ font-size: 25px; font-weight: 800; margin: 12px 0 12px; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
.ix-date {{ font-size: 11px; color: var(--muted); font-weight: 500; margin-left: 8px; }}
.ix-metrics {{ display: flex; flex-direction: column; gap: 0; }}
.ix-metric {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 12px; padding: 8px 0; border-top: 1px solid var(--border); }}
.ix-k {{ color: var(--ink-2); }}
.ix-v {{ text-align: right; font-variant-numeric: tabular-nums; }}
.ix-v b {{ font-weight: 700; }}
.ix-gate {{ font-size: 10px; font-weight: 800; padding: 1px 7px; border-radius: 5px; }}
.ix-gate.pos {{ background: rgba(34,197,94,0.15); }}
.ix-gate.neg {{ background: rgba(239,68,68,0.14); }}
.ix-paper {{ font-size: 10px; color: var(--muted); font-weight: 500; text-transform: none; letter-spacing: 0; margin-left: 8px; }}
.sig-badge {{
    display: inline-block; padding: 4px 11px; border-radius: 999px;
    font-size: 11px; font-weight: 800; letter-spacing: 0.4px; white-space: nowrap;
}}
.sig-enter {{ background: rgba(34,197,94,0.16); color: var(--pos); border: 1px solid rgba(34,197,94,0.35); }}
.sig-hold {{ background: rgba(57,135,229,0.16); color: #6aa9f2; border: 1px solid rgba(57,135,229,0.35); }}
.sig-exit {{ background: rgba(239,68,68,0.16); color: var(--neg); border: 1px solid rgba(239,68,68,0.35); }}
.sig-flat {{ background: rgba(255,255,255,0.05); color: var(--ink-2); border: 1px solid var(--border); }}

/* Bar rows */
.bar-row {{ display: flex; align-items: center; gap: 8px; margin: 7px 0; font-size: 12px; }}
.bar-label {{ width: 96px; flex-shrink: 0; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.bar-track {{ flex: 1; background: var(--tile-2); border-radius: 5px; height: 14px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 5px; }}
.bar-fill.pos {{ background: var(--pos); }}
.bar-fill.neg {{ background: var(--neg); }}
.bar-value {{ width: 108px; text-align: right; flex-shrink: 0; font-weight: 700; font-variant-numeric: tabular-nums; }}

/* Section label */
.section-label {{ font-size: 12px; letter-spacing: 1.2px; color: var(--ink-2); font-weight: 700; text-transform: uppercase; margin: 4px 0 12px; }}

/* Risk section */
.risk-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }}
.risk-card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px; min-width: 0;
}}
.risk-card h3:first-child {{ margin-top: 0; }}

/* Footer */
.footer {{ text-align: center; color: var(--muted); font-size: 11px; margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--border); }}

@media (max-width: 1024px) {{
    .bots {{ grid-template-columns: 1fr; }}
    .ix-cards {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: repeat(2, 1fr); }}
    .bot-stats {{ grid-template-columns: repeat(2, 1fr); }}
    .risk-grid {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 600px) {{
    body {{ padding: 14px; }}
    .risk-grid {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: 1fr 1fr; }}
    .overview-card .value {{ font-size: 24px; }}
    .header h1 {{ font-size: 19px; }}
    .hero {{ padding: 18px; gap: 16px; }}
    .hero-value {{ font-size: 34px; }}
    .hero-breakdown {{ width: 100%; }}
    .hb {{ flex: 1 1 100%; }}
}}
</style>
</head>
<body>

<div class="header">
    <div class="brand">
        <span class="dot"></span>
        <h1>Trading <span>Admin</span></h1>
    </div>
    <div class="chips">
        <span class="chip">{now}</span>
        <span class="chip"><b>{bots_online}/3</b> bots online</span>
        <span class="chip">Regime <b class="regime-{regime_label.lower()}">{regime_label}</b></span>
        <span class="chip">VIX <b class="yellow">{vix_label}</b></span>
    </div>
</div>

{hero_html}

<div class="overview">
    <div class="overview-card {'k-pnl' if total_pnl >= 0 else 'k-neg'}">
        <div class="label">Total P&L</div>
        <div class="value num {'green' if total_pnl >= 0 else 'red'}">${total_pnl:+,.2f}</div>
        <div class="sub">FX ${fx_total_pnl:+,.0f} &middot; Stock ${stock_pnl:+,.0f} &middot; Poly ${poly_pnl:+,.0f}</div>
    </div>
    <div class="overview-card k-open">
        <div class="label">Total Open Positions</div>
        <div class="value num blue">{total_open}</div>
        <div class="sub">FX {fx['open_count']} &middot; Stock {len(stock.get('open_positions', [])) if stock else 0} &middot; Poly {(poly['ev']['open'] + poly['weather']['open']) if poly else 0}</div>
    </div>
    <div class="overview-card k-regime">
        <div class="label">Market Regime</div>
        <div class="value regime-{regime_label.lower()}">{regime_label}</div>
        <div class="sub">{regime_desc[:60]}</div>
    </div>
    <div class="overview-card k-vix">
        <div class="label">VIX</div>
        <div class="value num yellow">{vix_label}</div>
        <div class="sub">{vix_desc}</div>
    </div>
</div>

<div class="chart-container">
    <div class="chart-header">
        <span class="chart-title">Portfolio Equity</span>
        <span class="chart-legend"><span class="swatch"></span> Stock bot equity &middot; $100k baseline</span>
    </div>
    <div id="chart">{equity_svg}</div>
</div>

<div class="section-label">Bots</div>
<div class="bots">
    {stock_html if stock_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">STOCK/ETF BOT</div></div><div class="empty">No data available</div></div>'}
    {fx_html}
    {poly_html if poly_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">POLYMARKET BOT</div></div><div class="empty">No data available</div></div>'}
</div>

{index_html}

<div class="section-label">Risk &amp; Exposure</div>
{risk_html}

{_build_api_health_html(api_health)}

<div class="footer">Trading Admin Dashboard &middot; Auto-generated {now} &middot; FX regime source shared/global_state.json &middot; Stock: {stock.get('source','offline') if stock else 'offline'} &middot; Poly: {poly.get('source','offline') if poly else 'offline'} &middot; Index: shared/pipeline.db (dry-run)</div>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def serve(port: int = 8050):
    """Run local dashboard server."""
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    from urllib.parse import urlparse

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/chart":
                self.send_response(404)
                self.end_headers()
            else:
                fx = get_fx_data()
                stock = get_stock_data()
                poly = get_poly_data()
                try:
                    from pipeline.agents.api_health import check_all
                    api_health = check_all()
                except Exception:
                    api_health = None
                html = build_dashboard(fx, stock, poly, api_health)
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", port), Handler)
    print(f"Dashboard at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def generate_static(output: str = "docs/index.html"):
    """Generate static HTML dashboard."""
    print("Generating static dashboard...")

    print("  Fetching FX data...")
    fx = get_fx_data()

    print("  Fetching stock data...")
    stock = get_stock_data()

    print("  Fetching polymarket data...")
    poly = get_poly_data()

    print("  Running API health checks...")
    try:
        from pipeline.agents.api_health import check_all
        api_health = check_all()
    except Exception as e:
        print(f"    Health check failed: {e}")
        api_health = None

    html = build_dashboard(fx, stock, poly, api_health)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"  Dashboard written to {output}")


def main():
    parser = argparse.ArgumentParser(description="Trading Admin Dashboard")
    parser.add_argument("--serve", action="store_true", help="Run local web server")
    parser.add_argument("--static", action="store_true", help="Generate static HTML")
    parser.add_argument("--output", default="docs/index.html", help="Output path for static mode")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()

    if args.serve:
        serve(port=args.port)
    elif args.static:
        generate_static(output=args.output)
    else:
        serve(port=args.port)


if __name__ == "__main__":
    main()
