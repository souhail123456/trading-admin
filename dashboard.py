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


def _fetch_stock_local() -> dict | None:
    """Fallback: shared/daily_history.jsonl stock daily_snapshot events (always
    committed by the pipeline), optionally enriched by a local trading-bot clone."""
    dh = load_daily_history()
    snaps = dh["stock_snapshots"]
    if not snaps:
        return None
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
# Inline SVG / CSS chart builders (no external JS libraries)
# ---------------------------------------------------------------------------

def render_equity_svg(history: list[dict], baseline: float = 100000,
                       width: int = 1080, height: int = 260) -> str:
    """Stock portfolio equity curve as a self-contained inline SVG line+area chart."""
    if not history or len(history) < 2:
        return '<div class="empty" style="padding:60px 0">No equity history available yet</div>'

    values = [h["value"] for h in history]
    n = len(values)
    lo, hi = min(values + [baseline]), max(values + [baseline])
    pad = (hi - lo) * 0.08 or max(hi * 0.02, 1)
    lo, hi = lo - pad, hi + pad

    pad_l, pad_r, pad_t, pad_b = 58, 16, 16, 26
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
        grid.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" stroke="#1e2a3a" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="10" fill="#666">${val:,.0f}</text>')

    x_labels = []
    for idx in sorted({0, n // 2, n - 1}):
        x_labels.append(f'<text x="{x(idx):.1f}" y="{height - 6}" text-anchor="middle" font-size="10" fill="#666">{history[idx]["time"]}</text>')

    step = max(1, n // 80)
    circles = []
    for i in range(0, n, step):
        px, py = pts[i]
        circles.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5" fill="#00d4aa"><title>{history[i]["time"]}: ${values[i]:,.2f}</title></circle>')
    if (n - 1) % step != 0:
        px, py = pts[-1]
        circles.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="#00d4aa"><title>{history[-1]["time"]}: ${values[-1]:,.2f}</title></circle>')

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" style="height:{height}px;display:block" role="img" aria-label="Stock portfolio equity curve">
      <defs>
        <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#00d4aa" stop-opacity="0.30"/>
          <stop offset="100%" stop-color="#00d4aa" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      {''.join(grid)}
      <line x1="{pad_l}" y1="{baseline_y:.1f}" x2="{width - pad_r}" y2="{baseline_y:.1f}" stroke="#666" stroke-width="1" stroke-dasharray="4,4"/>
      <text x="{width - pad_r}" y="{baseline_y - 4:.1f}" text-anchor="end" font-size="10" fill="#888">${baseline:,.0f} baseline</text>
      <path d="{area_d}" fill="url(#eqFill)" stroke="none"/>
      <path d="{path_d}" fill="none" stroke="#00d4aa" stroke-width="2"/>
      {''.join(circles)}
      {''.join(x_labels)}
    </svg>'''


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
        <table>
            <tr><th>Service</th><th>Bot</th><th>Status</th><th>Latency</th><th>Details</th><th>Fallback</th></tr>
            {rows}
        </table>
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


def build_dashboard(fx: dict, stock: dict | None, poly: dict | None,
                    api_health: list[dict] | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = get_global_state()

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
        <table>
            <tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>P&L</th><th>Held</th><th>Swap Est.</th></tr>
            {fx_rows}
        </table>
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
            <table>
                <tr><th>Symbol</th><th>Side</th><th>Shares</th><th>Entry</th><th>Unrealized</th></tr>
                {pos_html}
            </table>
            {f'<h3>Closed Trades</h3><table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th></tr>{closed_html}</table>' if closed_html else ''}
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
            <table>
                <tr><th>Market</th><th>Size</th><th>Status</th><th>P&L</th></tr>
                {recent_html}
            </table>
        </div>"""

    risk_html = _build_risk_html(fx, stock, state)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Admin Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #0a0e17;
    color: #e0e0e0;
    padding: 20px;
    max-width: 1400px;
    margin: 0 auto;
}}

/* Header */
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid #1e2a3a;
}}
.header h1 {{ color: #00d4aa; font-size: 24px; }}
.header .meta {{ color: #666; font-size: 13px; }}

/* Overview cards */
.overview {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 24px;
}}
.overview-card {{
    background: #111827;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 18px;
}}
.overview-card .label {{ color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
.overview-card .value {{ font-size: 28px; font-weight: bold; margin-top: 6px; }}
.overview-card .sub {{ font-size: 12px; color: #555; margin-top: 4px; }}

/* Colors */
.pos {{ color: #00d4aa; }}
.neg {{ color: #ef4444; }}
.green {{ color: #00d4aa; }}
.blue {{ color: #3b82f6; }}
.yellow {{ color: #f59e0b; }}
.red {{ color: #ef4444; }}
.dim {{ color: #555; }}

/* Regime */
.regime-trending {{ color: #00d4aa; }}
.regime-ranging {{ color: #f59e0b; }}
.regime-volatile {{ color: #ef4444; }}
.regime-crisis {{ color: #ef4444; font-weight: bold; }}
.regime-n\\/a {{ color: #666; }}

/* Chart */
.chart-container {{
    background: #111827;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    margin-bottom: 24px;
    overflow: hidden;
}}
.chart-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 16px;
    border-bottom: 1px solid #1e2a3a;
}}
.chart-title {{ color: #00d4aa; font-size: 16px; font-weight: bold; }}
#chart {{ padding: 8px 4px 0; }}

/* Bot cards */
.bots {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
.bot-card {{
    background: #111827;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 16px;
    overflow: hidden;
}}
.bot-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
    padding-bottom: 10px;
    border-bottom: 1px solid #1e2a3a;
    flex-wrap: wrap;
    gap: 6px;
}}
.bot-title {{ font-size: 14px; font-weight: bold; }}
.bot-tag {{
    background: #1e2a3a;
    color: #888;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: normal;
    margin-left: 6px;
}}
.bot-pnl {{ font-size: 18px; font-weight: bold; }}
.market-status {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: bold;
    margin-left: 6px;
}}
.market-open {{ background: #0d3320; color: #00d4aa; }}
.market-closed {{ background: #3b1111; color: #ef4444; }}
.market-247 {{ background: #1a1a33; color: #8b5cf6; }}
.live-tag {{ background: #0d3320; color: #00d4aa; }}
.offline-tag {{ background: #33260d; color: #f59e0b; }}
.bot-stats {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 14px;
}}
.mini-stat {{
    background: #0d1520;
    border-radius: 6px;
    padding: 8px 10px;
}}
.mini-stat .label {{ display: block; font-size: 10px; color: #555; text-transform: uppercase; }}
.mini-stat .val {{ display: block; font-size: 13px; font-weight: bold; margin-top: 2px; }}

h3 {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #555;
    margin: 14px 0 8px;
}}

/* Tables */
table {{ width: 100%; border-collapse: collapse; }}
th {{
    text-align: left;
    padding: 5px 8px;
    font-size: 10px;
    text-transform: uppercase;
    color: #444;
    border-bottom: 1px solid #1e2a3a;
}}
td {{ padding: 6px 8px; font-size: 12px; border-bottom: 1px solid #0d1520; }}
tr:hover {{ background: #1a2332; }}
.empty {{ text-align: center; color: #555; padding: 16px !important; }}

.badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: bold;
}}
.badge-t {{ background: #0d3320; color: #00d4aa; }}
.badge-pa {{ background: #1e1a33; color: #8b5cf6; }}
.flag-badge {{
    display: inline-block;
    background: #3b1111;
    color: #ef4444;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: bold;
    margin: 2px 4px 2px 0;
}}

.sig-entry {{ color: #00d4aa; font-weight: bold; }}
.sig-exit {{ color: #ef4444; font-weight: bold; }}

/* Bar rows (winners/losers, currency exposure, weather side P&L) */
.bar-row {{ display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 12px; }}
.bar-label {{ width: 96px; flex-shrink: 0; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.bar-track {{ flex: 1; background: #0d1520; border-radius: 4px; height: 14px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 4px; }}
.bar-fill.pos {{ background: #00d4aa; }}
.bar-fill.neg {{ background: #ef4444; }}
.bar-value {{ width: 110px; text-align: right; flex-shrink: 0; font-weight: bold; }}

/* Risk section */
.risk-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
.risk-card {{
    background: #111827;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 16px;
}}
.risk-card h3:first-child {{ margin-top: 0; }}

/* Footer */
.footer {{ text-align: center; color: #333; font-size: 11px; margin-top: 24px; padding-top: 16px; border-top: 1px solid #1e2a3a; }}

@media (max-width: 1000px) {{
    .bots {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: repeat(2, 1fr); }}
    .bot-stats {{ grid-template-columns: repeat(2, 1fr); }}
    .risk-grid {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 600px) {{
    .risk-grid {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: 1fr 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
    <h1>Trading Admin</h1>
    <div class="meta">{now} | {bots_online}/3 bots online</div>
</div>

<div class="overview">
    <div class="overview-card">
        <div class="label">Total P&L</div>
        <div class="value {'green' if total_pnl >= 0 else 'red'}">${total_pnl:+,.2f}</div>
        <div class="sub">FX ${fx_total_pnl:+,.0f} &middot; Stock ${stock_pnl:+,.0f} &middot; Poly ${poly_pnl:+,.0f}</div>
    </div>
    <div class="overview-card">
        <div class="label">Total Open Positions</div>
        <div class="value blue">{total_open}</div>
        <div class="sub">FX {fx['open_count']} &middot; Stock {len(stock.get('open_positions', [])) if stock else 0} &middot; Poly {(poly['ev']['open'] + poly['weather']['open']) if poly else 0}</div>
    </div>
    <div class="overview-card">
        <div class="label">Regime</div>
        <div class="value regime-{regime_label.lower()}">{regime_label}</div>
        <div class="sub">{regime_desc[:60]}</div>
    </div>
    <div class="overview-card">
        <div class="label">VIX</div>
        <div class="value yellow">{vix_label}</div>
        <div class="sub">{vix_desc}</div>
    </div>
</div>

<div class="chart-container">
    <div class="chart-header">
        <span class="chart-title">Portfolio Equity</span>
        <span style="color:#666;font-size:12px">Stock bot — $100k baseline</span>
    </div>
    <div id="chart">{equity_svg}</div>
</div>

<div class="bots">
    {stock_html if stock_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">STOCK/ETF BOT</div></div><div class="empty">No data available</div></div>'}
    {fx_html}
    {poly_html if poly_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">POLYMARKET BOT</div></div><div class="empty">No data available</div></div>'}
</div>

<h3 style="font-size:13px;letter-spacing:1px;color:#888;margin-bottom:12px">RISK &amp; EXPOSURE</h3>
{risk_html}

{_build_api_health_html(api_health)}

<div class="footer">Trading Admin Dashboard | Auto-generated {now} | FX: {fx.get('regime',{}).get('regime','?')} regime source shared/global_state.json | Stock: {stock.get('source','offline') if stock else 'offline'} | Poly: {poly.get('source','offline') if poly else 'offline'}</div>

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
