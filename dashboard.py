"""
Trading Admin — Unified Dashboard
----------------------------------
Web dashboard covering all 3 bots with charts and live data.

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
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.db import init_db
from pipeline.agents.data_fetcher import fetch_ohlcv, CURRENCY_PAIRS


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def get_fx_data(conn: sqlite3.Connection) -> dict:
    """Get FX bot data from local pipeline.db."""
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at"
    ).fetchall()
    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101) ORDER BY closed_at DESC LIMIT 20"
    ).fetchall()
    signals = conn.execute(
        "SELECT * FROM signals WHERE strategy_id IN (100, 101) ORDER BY generated_at DESC LIMIT 10"
    ).fetchall()
    all_closed = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
    ).fetchall()

    realized = sum(dict(t).get("pnl", 0) or 0 for t in all_closed)
    wins = sum(1 for t in all_closed if (dict(t).get("pnl", 0) or 0) > 0)
    total = len(all_closed)

    # Last regime
    regime_row = conn.execute(
        "SELECT outputs FROM agent_log WHERE agent = 'regime_detector' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    regime = None
    if regime_row:
        try:
            regime = json.loads(dict(regime_row)["outputs"])
        except Exception:
            pass

    # Performance snapshots
    perf_rows = conn.execute(
        "SELECT * FROM agent_log WHERE agent = 'performance_monitor' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "open_trades": [dict(t) for t in open_trades],
        "closed_trades": [dict(t) for t in closed_trades],
        "signals": [dict(s) for s in signals],
        "realized_pnl": round(realized, 2),
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "wins": wins,
        "total_closed": total,
        "open_count": len(open_trades),
        "regime": regime,
    }


def get_stock_data() -> dict:
    """Get stock bot data via GitHub API."""
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        return None
    try:
        from github import Github
        gh = Github(gh_token)
        repo = gh.get_repo(os.environ.get("STOCK_REPO", "souhail123456/trading-bot"))
        content = repo.get_contents("memory/TRADE-LOG.md").decoded_content.decode()

        import re
        match = re.search(r"<!--\s*SUMMARY\s*\n(.*?)-->", content, re.DOTALL)
        if not match:
            return None

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

        # Last run
        last_run = None
        try:
            runs = repo.get_workflow_runs(status="completed")
            if runs.totalCount > 0:
                last_run = runs[0].created_at.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

        return {
            "portfolio_value": summary.get("portfolio_value", 100000),
            "cash": summary.get("cash", 100000),
            "total_pnl": summary.get("total_pnl", 0),
            "open_positions": summary.get("open_positions", []),
            "closed_trades": summary.get("closed_trades", []),
            "last_run": last_run,
        }
    except Exception as e:
        print(f"  Stock data fetch failed: {e}")
        return None


def get_poly_data() -> dict:
    """Get polymarket data via GitHub API."""
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

        def stats(trades):
            resolved = [t for t in trades if t.get("resolved")]
            wins = [t for t in resolved if t.get("won")]
            pnl = sum(float(t.get("realized_pnl", 0)) for t in resolved)
            risked = sum(float(t.get("size_usd", 0)) for t in trades)
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

        # Last run
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
            "ev_recent": ev_trades[-5:],
            "weather_recent": weather_trades[-5:],
            "last_run": last_run,
        }
    except Exception as e:
        print(f"  Polymarket data fetch failed: {e}")
        return None


def get_chart_data(symbol: str, days: int = 90) -> dict:
    """Get OHLCV data for charting."""
    yf_ticker = symbol + "=X"
    for ticker in CURRENCY_PAIRS:
        if ticker.replace("=X", "") == symbol:
            yf_ticker = ticker
            break

    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = fetch_ohlcv([yf_ticker], start=start, cache=False)

    if yf_ticker not in data or data[yf_ticker].empty:
        return {"candles": [], "sma": []}

    df = data[yf_ticker]
    candles = []
    for date, row in df.iterrows():
        candles.append({
            "time": date.strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 5),
            "high": round(float(row["high"]), 5),
            "low": round(float(row["low"]), 5),
            "close": round(float(row["close"]), 5),
        })

    sma_data = []
    if len(df) >= 200:
        sma_200 = df["close"].rolling(200).mean()
        for date, val in sma_200.dropna().items():
            sma_data.append({"time": date.strftime("%Y-%m-%d"), "value": round(float(val), 5)})

    return {"candles": candles, "sma": sma_data}


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_dashboard(fx: dict, stock: dict | None, poly: dict | None, chart_data: dict | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Aggregate totals
    bots_online = 1  # FX always online
    total_pnl = fx["realized_pnl"]
    if stock:
        bots_online += 1
        total_pnl += stock.get("total_pnl", 0)
    if poly:
        bots_online += 1
        total_pnl += poly["ev"]["pnl"] + poly["weather"]["pnl"]

    regime = fx.get("regime") or {}
    regime_label = regime.get("regime", "N/A")
    regime_desc = regime.get("description", "")
    vix = regime.get("vix", "N/A")

    # Chart JSON
    chart_json = json.dumps(chart_data or {"candles": [], "sma": []})

    # FX position rows
    fx_rows = ""
    for t in fx["open_trades"]:
        tag = "T" if t["strategy_id"] == 100 else "PA"
        days = ""
        if t.get("opened_at"):
            try:
                d = (datetime.now() - datetime.strptime(t["opened_at"][:19], "%Y-%m-%dT%H:%M:%S")).days
                days = f"{d}d"
            except Exception:
                pass
        fx_rows += f"""<tr>
            <td><span class="badge badge-{tag.lower()}">{tag}</span> <b>{t['symbol']}</b></td>
            <td>{t['side'].upper()}</td>
            <td>{t.get('quantity', '?')}</td>
            <td>{t['entry_price']}</td>
            <td>{days}</td>
        </tr>"""
    if not fx_rows:
        fx_rows = '<tr><td colspan="5" class="empty">No open positions</td></tr>'

    # FX signal rows
    signal_rows = ""
    for s in fx.get("signals", [])[:8]:
        state = json.loads(s["full_state"]) if isinstance(s["full_state"], str) else (s["full_state"] or {})
        tag = "T" if s["strategy_id"] == 100 else "PA"
        css = "entry" if s["signal_type"] == "entry" else "exit"
        signal_rows += f"""<tr>
            <td>{str(s['generated_at'])[:10]}</td>
            <td><span class="badge badge-{tag.lower()}">{tag}</span></td>
            <td><b>{s['symbol']}</b></td>
            <td class="sig-{css}">{s['signal_type'].upper()}</td>
            <td>{s['price_at_signal']}</td>
        </tr>"""

    # Stock section
    stock_html = ""
    if stock:
        pv = stock["portfolio_value"]
        pnl = stock["total_pnl"]
        pnl_pct = (pnl / 100000) * 100
        pos_html = ""
        for p in stock.get("open_positions", []):
            unrealized = p.get("unrealized_pnl", 0)
            pos_html += f"""<tr>
                <td><b>{p['symbol']}</b></td>
                <td>{p.get('side', 'BUY')}</td>
                <td>{p['shares']}</td>
                <td>${p['entry']}</td>
                <td class="{'pos' if unrealized >= 0 else 'neg'}">${unrealized:+,.2f}</td>
            </tr>"""
        if not pos_html:
            pos_html = '<tr><td colspan="5" class="empty">No open positions</td></tr>'

        closed_html = ""
        for t in stock.get("closed_trades", [])[:5]:
            rpnl = t.get("realized_pnl", 0)
            closed_html += f"""<tr>
                <td>{t.get('symbol','?')}</td>
                <td>${t.get('entry','?')}</td>
                <td>${t.get('exit','?')}</td>
                <td class="{'pos' if rpnl >= 0 else 'neg'}">${rpnl:+,.2f}</td>
            </tr>"""

        stock_html = f"""
        <div class="bot-card">
            <div class="bot-header">
                <div class="bot-title">STOCK/ETF BOT <span class="bot-tag">Alpaca</span></div>
                <div class="bot-pnl {'pos' if pnl >= 0 else 'neg'}">${pnl:+,.2f} ({pnl_pct:+.2f}%)</div>
            </div>
            <div class="bot-stats">
                <div class="mini-stat"><span class="label">Portfolio</span><span class="val">${pv:,.2f}</span></div>
                <div class="mini-stat"><span class="label">Cash</span><span class="val">${stock.get('cash', 0):,.2f}</span></div>
                <div class="mini-stat"><span class="label">Positions</span><span class="val">{len(stock.get('open_positions', []))}</span></div>
                <div class="mini-stat"><span class="label">Last Run</span><span class="val">{stock.get('last_run', 'N/A')}</span></div>
            </div>
            <table>
                <tr><th>Symbol</th><th>Side</th><th>Shares</th><th>Entry</th><th>Unrealized</th></tr>
                {pos_html}
            </table>
            {f'<h3>Closed Trades</h3><table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th></tr>{closed_html}</table>' if closed_html else ''}
        </div>"""

    # Polymarket section
    poly_html = ""
    if poly:
        ev = poly["ev"]
        w = poly["weather"]
        total_poly_pnl = ev["pnl"] + w["pnl"]

        # Recent trades
        recent_html = ""
        for t in (poly.get("weather_recent", []) + poly.get("ev_recent", []))[-6:]:
            q = (t.get("market") or t.get("question", "?"))[:40]
            size = float(t.get("size_usd", 0))
            st = "WON" if t.get("won") else "LOST" if t.get("resolved") else "OPEN"
            rpnl = float(t.get("realized_pnl", 0) or 0)
            css = "pos" if rpnl > 0 else "neg" if rpnl < 0 else ""
            recent_html += f'<tr><td>{q}</td><td>${size:.2f}</td><td>{st}</td><td class="{css}">${rpnl:+.2f}</td></tr>'

        poly_html = f"""
        <div class="bot-card">
            <div class="bot-header">
                <div class="bot-title">POLYMARKET BOT <span class="bot-tag">Paper</span></div>
                <div class="bot-pnl {'pos' if total_poly_pnl >= 0 else 'neg'}">${total_poly_pnl:+,.2f}</div>
            </div>
            <div class="bot-stats">
                <div class="mini-stat"><span class="label">EV Trades</span><span class="val">{ev['total']} ({ev['open']} open)</span></div>
                <div class="mini-stat"><span class="label">EV Win Rate</span><span class="val">{ev['win_rate']}%</span></div>
                <div class="mini-stat"><span class="label">EV P&L</span><span class="val">${ev['pnl']:+.2f}</span></div>
                <div class="mini-stat"><span class="label">Weather Trades</span><span class="val">{w['total']} ({w['open']} open)</span></div>
                <div class="mini-stat"><span class="label">Weather Win Rate</span><span class="val">{w['win_rate']}%</span></div>
                <div class="mini-stat"><span class="label">Weather P&L</span><span class="val">${w['pnl']:+.2f}</span></div>
            </div>
            <h3>Recent Trades</h3>
            <table>
                <tr><th>Market</th><th>Size</th><th>Status</th><th>P&L</th></tr>
                {recent_html}
            </table>
        </div>"""

    # FX section
    fx_pnl = fx["realized_pnl"]
    fx_html = f"""
    <div class="bot-card">
        <div class="bot-header">
            <div class="bot-title">FX BOT <span class="bot-tag">Capital.com</span></div>
            <div class="bot-pnl {'pos' if fx_pnl >= 0 else 'neg'}">${fx_pnl:+,.2f}</div>
        </div>
        <div class="bot-stats">
            <div class="mini-stat"><span class="label">Open</span><span class="val">{fx['open_count']}</span></div>
            <div class="mini-stat"><span class="label">Closed</span><span class="val">{fx['total_closed']}</span></div>
            <div class="mini-stat"><span class="label">Win Rate</span><span class="val">{fx['win_rate']}%</span></div>
            <div class="mini-stat"><span class="label">Regime</span><span class="val regime-{regime_label.lower()}">{regime_label}</span></div>
        </div>
        <h3>Open Positions</h3>
        <table>
            <tr><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>Held</th></tr>
            {fx_rows}
        </table>
        <h3>Recent Signals</h3>
        <table>
            <tr><th>Date</th><th>Strategy</th><th>Symbol</th><th>Type</th><th>Price</th></tr>
            {signal_rows}
        </table>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Admin Dashboard</title>
<script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
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
#chart {{ height: 350px; }}

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

.sig-entry {{ color: #00d4aa; font-weight: bold; }}
.sig-exit {{ color: #ef4444; font-weight: bold; }}

/* Regime bar */
.regime-bar {{
    background: #111827;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 12px 18px;
    margin-bottom: 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
.regime-bar .regime-label {{ font-size: 14px; }}
.regime-bar .regime-detail {{ font-size: 12px; color: #666; }}

/* Footer */
.footer {{ text-align: center; color: #333; font-size: 11px; margin-top: 24px; padding-top: 16px; border-top: 1px solid #1e2a3a; }}

@media (max-width: 1000px) {{
    .bots {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: repeat(2, 1fr); }}
    .bot-stats {{ grid-template-columns: repeat(2, 1fr); }}
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
    </div>
    <div class="overview-card">
        <div class="label">Regime</div>
        <div class="value regime-{regime_label.lower()}">{regime_label}</div>
        <div class="sub">VIX: {vix}</div>
    </div>
    <div class="overview-card">
        <div class="label">FX Win Rate</div>
        <div class="value yellow">{fx['win_rate']}%</div>
        <div class="sub">{fx['wins']}/{fx['total_closed']} trades</div>
    </div>
    <div class="overview-card">
        <div class="label">Bots Online</div>
        <div class="value blue">{bots_online}/3</div>
    </div>
</div>

<div class="chart-container">
    <div class="chart-header">
        <span class="chart-title">EURUSD</span>
    </div>
    <div id="chart"></div>
</div>

<div class="bots">
    {stock_html if stock_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">STOCK/ETF BOT</div></div><div class="empty">Offline — no GH_TOKEN</div></div>'}
    {fx_html}
    {poly_html if poly_html else '<div class="bot-card"><div class="bot-header"><div class="bot-title">POLYMARKET BOT</div></div><div class="empty">Offline — no GH_TOKEN</div></div>'}
</div>

<div class="footer">Trading Admin Dashboard | Auto-generated {now}</div>

<script>
const chartEl = document.getElementById('chart');
if (chartEl) {{
    const chart = LightweightCharts.createChart(chartEl, {{
        width: chartEl.clientWidth,
        height: 350,
        layout: {{ background: {{ color: '#111827' }}, textColor: '#888' }},
        grid: {{ vertLines: {{ color: '#1e2a3a' }}, horzLines: {{ color: '#1e2a3a' }} }},
        rightPriceScale: {{ borderColor: '#1e2a3a' }},
        timeScale: {{ borderColor: '#1e2a3a', timeVisible: false }},
    }});

    const cd = {chart_json};
    if (cd.candles && cd.candles.length > 0) {{
        const cs = chart.addCandlestickSeries({{
            upColor: '#00d4aa', downColor: '#ef4444',
            borderUpColor: '#00d4aa', borderDownColor: '#ef4444',
            wickUpColor: '#00d4aa', wickDownColor: '#ef4444',
        }});
        cs.setData(cd.candles);
        if (cd.sma && cd.sma.length > 0) {{
            const sma = chart.addLineSeries({{ color: '#f59e0b', lineWidth: 2, title: 'SMA 200' }});
            sma.setData(cd.sma);
        }}
        chart.timeScale().fitContent();
    }}

    window.addEventListener('resize', () => chart.applyOptions({{ width: chartEl.clientWidth }}));
}}
</script>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def serve(port: int = 8050):
    """Run local dashboard server."""
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/chart":
                params = parse_qs(parsed.query)
                symbol = params.get("symbol", ["EURUSD"])[0]
                data = get_chart_data(symbol, days=120)
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            else:
                conn = init_db()
                fx = get_fx_data(conn)
                stock = get_stock_data()
                poly = get_poly_data()
                chart = get_chart_data("EURUSD", days=120)
                html = build_dashboard(fx, stock, poly, chart)
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
    conn = init_db()

    print("  Fetching FX data...")
    fx = get_fx_data(conn)

    print("  Fetching stock data...")
    stock = get_stock_data()

    print("  Fetching polymarket data...")
    poly = get_poly_data()

    print("  Fetching chart data...")
    chart = get_chart_data("EURUSD", days=120)

    html = build_dashboard(fx, stock, poly, chart)

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
