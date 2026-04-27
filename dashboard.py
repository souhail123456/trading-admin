"""
Trading Dashboard
-----------------
Local web dashboard with live charts and position tracking.
Uses TradingView Lightweight Charts (CDN) for candlestick charts.

Usage:
    python3 dashboard.py
    Open http://localhost:8050
"""

import json
import sqlite3
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import pandas as pd

from pipeline.db import init_db
from pipeline.agents.data_fetcher import fetch_ohlcv, CURRENCY_PAIRS


def get_dashboard_data() -> dict:
    conn = init_db()

    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at"
    ).fetchall()

    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101) ORDER BY closed_at DESC LIMIT 20"
    ).fetchall()

    signals = conn.execute(
        "SELECT * FROM signals WHERE strategy_id IN (100, 101) ORDER BY generated_at DESC LIMIT 20"
    ).fetchall()

    etf_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id NOT IN (100, 101) ORDER BY opened_at"
    ).fetchall()

    all_closed = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
    ).fetchall()
    realized = sum(dict(t).get("pnl", 0) or 0 for t in all_closed)
    wins = sum(1 for t in all_closed if (dict(t).get("pnl", 0) or 0) > 0)
    total = len(all_closed)

    agent_log = conn.execute(
        "SELECT * FROM agent_log ORDER BY created_at DESC LIMIT 15"
    ).fetchall()

    survivors = conn.execute(
        "SELECT * FROM strategies WHERE status = 'backtest_pass' ORDER BY id"
    ).fetchall()

    return {
        "open_trades": [dict(t) for t in open_trades],
        "closed_trades": [dict(t) for t in closed_trades],
        "etf_trades": [dict(t) for t in etf_trades],
        "signals": [dict(s) for s in signals],
        "agent_log": [dict(a) for a in agent_log],
        "survivors": [dict(s) for s in survivors],
        "stats": {
            "realized_pnl": round(realized, 2),
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "wins": wins,
            "total_closed": total,
            "open_count": len(open_trades),
        },
    }


def get_chart_data(symbol: str, days: int = 90) -> list[dict]:
    """Get OHLCV data for charting."""
    # Map clean symbol to yfinance ticker
    yf_ticker = symbol + "=X" if "USD" in symbol or "EUR" in symbol or "GBP" in symbol or "JPY" in symbol or "CHF" in symbol or "AUD" in symbol or "NZD" in symbol or "CAD" in symbol else symbol

    # Check if it's in our currency pairs
    for ticker in CURRENCY_PAIRS:
        clean = ticker.replace("=X", "")
        if clean == symbol:
            yf_ticker = ticker
            break

    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = fetch_ohlcv([yf_ticker], start=start, cache=False)

    if yf_ticker not in data or data[yf_ticker].empty:
        return []

    df = data[yf_ticker]
    candles = []
    sma_200 = df["close"].rolling(200).mean() if len(df) >= 200 else pd.Series(dtype=float)

    for date, row in df.iterrows():
        candle = {
            "time": date.strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 5),
            "high": round(float(row["high"]), 5),
            "low": round(float(row["low"]), 5),
            "close": round(float(row["close"]), 5),
        }
        candles.append(candle)

    # SMA data
    sma_data = []
    if len(df) >= 200:
        sma_200 = df["close"].rolling(200).mean()
        for date, val in sma_200.dropna().items():
            sma_data.append({
                "time": date.strftime("%Y-%m-%d"),
                "value": round(float(val), 5),
            })

    return {"candles": candles, "sma": sma_data}


def get_live_prices(symbols: list[str]) -> dict:
    """Get latest prices for open positions."""
    prices = {}
    for symbol in symbols:
        yf_ticker = None
        for ticker in CURRENCY_PAIRS:
            if ticker.replace("=X", "") == symbol:
                yf_ticker = ticker
                break
        if not yf_ticker:
            continue
        start = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        data = fetch_ohlcv([yf_ticker], start=start, cache=False)
        if yf_ticker in data and not data[yf_ticker].empty:
            prices[symbol] = round(float(data[yf_ticker]["close"].iloc[-1]), 5)
    return prices


def build_html() -> str:
    data = get_dashboard_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Get live prices for open positions
    open_symbols = [t["symbol"] for t in data["open_trades"]]
    live_prices = get_live_prices(open_symbols) if open_symbols else {}

    # Build position rows with live P&L
    open_rows = ""
    total_unrealized = 0
    for t in data["open_trades"]:
        symbol = t["symbol"]
        entry = t["entry_price"]
        current = live_prices.get(symbol, entry)
        qty = t.get("quantity", 0) or 0

        # P&L calculation for FX micro lots
        # For JPY pairs: pip value is different
        if "JPY" in symbol:
            pips = (current - entry) * 100
            pnl = pips * qty * 0.01  # ~$0.01 per pip per micro lot for JPY pairs at this scale
        else:
            pips = (current - entry) * 10000
            pnl = pips * qty * 0.10  # ~$0.10 per pip per micro lot

        pnl_class = "pos" if pnl >= 0 else "neg"
        total_unrealized += pnl

        open_rows += f"""<tr class="position-row" data-symbol="{symbol}" onclick="showChart('{symbol}')">
            <td><strong>{symbol}</strong></td>
            <td>{t['side'].upper()}</td>
            <td>{qty:.0f}</td>
            <td>{entry}</td>
            <td>{current}</td>
            <td>{pips:+.1f}</td>
            <td class="{pnl_class}">${pnl:+.2f}</td>
            <td>{str(t.get('opened_at', ''))[:10]}</td>
        </tr>"""

    if not open_rows:
        open_rows = '<tr><td colspan="8" style="text-align:center;color:#888;padding:20px">No open FX positions</td></tr>'

    # Chart symbols for tabs
    chart_symbols = open_symbols if open_symbols else ["EURUSD", "GBPUSD", "USDJPY"]
    chart_tabs = ""
    for i, sym in enumerate(chart_symbols):
        active = "active" if i == 0 else ""
        chart_tabs += f'<button class="chart-tab {active}" onclick="showChart(\'{sym}\')">{sym}</button>'

    # Add all 10 pairs as extra tabs
    all_pairs = [t.replace("=X", "") for t in CURRENCY_PAIRS.keys()]
    for sym in all_pairs:
        if sym not in chart_symbols:
            chart_tabs += f'<button class="chart-tab" onclick="showChart(\'{sym}\')">{sym}</button>'

    # Signals
    signal_rows = ""
    for s in data["signals"]:
        state = json.loads(s["full_state"]) if isinstance(s["full_state"], str) else s["full_state"]
        strategy = "TREND" if s["strategy_id"] == 100 else "PA"
        css_class = "entry" if s["signal_type"] == "entry" else "exit"
        strength = state.get("trend_strength", state.get("bull_score", "—"))
        signal_rows += f"""<tr class="{css_class}">
            <td>{str(s['generated_at'])[:16]}</td>
            <td><span class="badge badge-{strategy.lower()}">{strategy}</span></td>
            <td><strong>{s['symbol']}</strong></td>
            <td class="sig-{s['signal_type']}">{s['signal_type'].upper()}</td>
            <td>{s['price_at_signal']}</td>
            <td>{strength}</td>
        </tr>"""

    # Closed trades
    closed_rows = ""
    for t in data["closed_trades"]:
        pnl = t.get("pnl", 0) or 0
        r = t.get("r_multiple", 0) or 0
        css = "win" if pnl > 0 else "loss" if pnl < 0 else ""
        closed_rows += f"""<tr class="{css}">
            <td>{t['symbol']}</td>
            <td>{t['entry_price']}</td>
            <td>{t.get('exit_price', '—')}</td>
            <td class="pnl">${pnl:+.2f}</td>
            <td>{r:+.1f}R</td>
            <td>{str(t.get('closed_at', ''))[:10]}</td>
        </tr>"""
    if not closed_rows:
        closed_rows = '<tr><td colspan="6" style="text-align:center;color:#888">No closed trades yet</td></tr>'

    # Agent log
    log_rows = ""
    for a in data["agent_log"]:
        log_rows += f"""<tr>
            <td>{str(a['created_at'])[:16]}</td>
            <td><span class="badge">{a['agent']}</span></td>
            <td>{a['action']}</td>
        </tr>"""

    # Survivors
    survivor_rows = ""
    for s in data["survivors"]:
        survivor_rows += f"""<tr>
            <td>{s['id']}</td>
            <td><strong>{s['name']}</strong></td>
            <td>{s['asset_universe'][:30]}</td>
        </tr>"""

    stats = data["stats"]
    portfolio_val = 10000 + stats["realized_pnl"] + total_unrealized
    default_chart = chart_symbols[0] if chart_symbols else "EURUSD"

    # Get initial chart data
    initial_chart = get_chart_data(default_chart, days=120)
    initial_candles_json = json.dumps(initial_chart.get("candles", []))
    initial_sma_json = json.dumps(initial_chart.get("sma", []))

    # Entry markers for chart
    entry_markers = []
    for t in data["open_trades"]:
        if t["symbol"] == default_chart:
            entry_markers.append({
                "time": str(t.get("opened_at", ""))[:10],
                "position": "belowBar",
                "color": "#00d4aa",
                "shape": "arrowUp",
                "text": f"LONG @ {t['entry_price']}",
            })
    markers_json = json.dumps(entry_markers)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FX Trading Pipeline</title>
<script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
        background: #0a0e17;
        color: #e0e0e0;
        padding: 16px;
    }}
    .header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 20px;
        padding-bottom: 12px;
        border-bottom: 1px solid #1e2a3a;
    }}
    .header h1 {{ color: #00d4aa; font-size: 22px; }}
    .header .time {{ color: #888; font-size: 13px; }}
    .header .mode {{
        background: #0d3320;
        color: #00d4aa;
        padding: 4px 12px;
        border-radius: 4px;
        font-size: 12px;
    }}
    .stats {{
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 12px;
        margin-bottom: 20px;
    }}
    .stat-card {{
        background: #111827;
        border: 1px solid #1e2a3a;
        border-radius: 8px;
        padding: 14px;
    }}
    .stat-card .label {{ color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .stat-card .value {{ font-size: 24px; font-weight: bold; margin-top: 4px; }}
    .green {{ color: #00d4aa; }}
    .blue {{ color: #3b82f6; }}
    .yellow {{ color: #f59e0b; }}
    .red {{ color: #ef4444; }}
    .pos {{ color: #00d4aa; font-weight: bold; }}
    .neg {{ color: #ef4444; font-weight: bold; }}

    .chart-container {{
        background: #111827;
        border: 1px solid #1e2a3a;
        border-radius: 8px;
        margin-bottom: 20px;
        overflow: hidden;
    }}
    .chart-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 8px 12px;
        border-bottom: 1px solid #1e2a3a;
    }}
    .chart-tabs {{
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
    }}
    .chart-tab {{
        background: #1e2a3a;
        color: #888;
        border: none;
        padding: 4px 10px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
        font-family: monospace;
    }}
    .chart-tab:hover {{ background: #2a3a4a; color: #fff; }}
    .chart-tab.active {{ background: #00d4aa; color: #0a0e17; }}
    #chart {{ height: 400px; }}
    .chart-symbol {{ color: #00d4aa; font-size: 16px; font-weight: bold; }}
    .chart-price {{ color: #fff; font-size: 14px; margin-left: 12px; }}

    .section {{
        background: #111827;
        border: 1px solid #1e2a3a;
        border-radius: 8px;
        margin-bottom: 16px;
        overflow: hidden;
    }}
    .section h2 {{
        padding: 10px 14px;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: #666;
        border-bottom: 1px solid #1e2a3a;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
        text-align: left;
        padding: 6px 10px;
        font-size: 10px;
        text-transform: uppercase;
        color: #555;
        border-bottom: 1px solid #1e2a3a;
    }}
    td {{ padding: 7px 10px; font-size: 12px; border-bottom: 1px solid #0d1520; }}
    tr:hover {{ background: #1a2332; }}
    .position-row {{ cursor: pointer; }}
    .position-row:hover {{ background: #1a3332 !important; }}
    .sig-entry {{ color: #00d4aa; font-weight: bold; }}
    .sig-exit {{ color: #ef4444; font-weight: bold; }}
    tr.win td.pnl {{ color: #00d4aa; }}
    tr.loss td.pnl {{ color: #ef4444; }}
    .badge {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 10px;
        font-weight: bold;
        background: #1e2a3a;
        color: #888;
    }}
    .badge-trend {{ background: #0d3320; color: #00d4aa; }}
    .badge-pa {{ background: #1e1a33; color: #8b5cf6; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .grid3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }}
    @media (max-width: 900px) {{
        .grid, .grid3 {{ grid-template-columns: 1fr; }}
        .stats {{ grid-template-columns: repeat(2, 1fr); }}
    }}
</style>
</head>
<body>

<div class="header">
    <h1>FX Trading Pipeline</h1>
    <span class="mode">DEMO — $10K</span>
    <div class="time">{now} | <a href="/" style="color:#3b82f6;text-decoration:none">Refresh</a></div>
</div>

<div class="stats">
    <div class="stat-card">
        <div class="label">Portfolio Value</div>
        <div class="value green">${portfolio_val:,.0f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Open Positions</div>
        <div class="value blue">{stats['open_count']}</div>
    </div>
    <div class="stat-card">
        <div class="label">Unrealized P&L</div>
        <div class="value {'green' if total_unrealized >= 0 else 'red'}">${total_unrealized:+,.2f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Realized P&L</div>
        <div class="value {'green' if stats['realized_pnl'] >= 0 else 'red'}">${stats['realized_pnl']:+,.2f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Win Rate</div>
        <div class="value yellow">{stats['win_rate']}% <span style="font-size:12px;color:#666">({stats['wins']}/{stats['total_closed']})</span></div>
    </div>
</div>

<div class="chart-container">
    <div class="chart-header">
        <div>
            <span class="chart-symbol" id="chart-symbol">{default_chart}</span>
            <span class="chart-price" id="chart-price"></span>
        </div>
        <div class="chart-tabs">
            {chart_tabs}
        </div>
    </div>
    <div id="chart"></div>
</div>

<div class="section">
    <h2>Open FX Positions (click to view chart)</h2>
    <table>
        <tr><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>Current</th><th>Pips</th><th>P&L</th><th>Opened</th></tr>
        {open_rows}
    </table>
</div>

<div class="grid">
    <div class="section">
        <h2>Recent Signals</h2>
        <table>
            <tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Type</th><th>Price</th><th>Strength</th></tr>
            {signal_rows}
        </table>
    </div>
    <div class="section">
        <h2>Trade History</h2>
        <table>
            <tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th>Date</th></tr>
            {closed_rows}
        </table>
    </div>
</div>

<div class="grid3">
    <div class="section">
        <h2>Strategy Survivors</h2>
        <table>
            <tr><th>ID</th><th>Name</th><th>Universe</th></tr>
            {survivor_rows}
        </table>
    </div>
    <div class="section" style="grid-column: span 2">
        <h2>Agent Activity</h2>
        <table>
            <tr><th>Time</th><th>Agent</th><th>Action</th></tr>
            {log_rows}
        </table>
    </div>
</div>

<script>
// Chart setup
const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {{
    width: chartEl.clientWidth,
    height: 400,
    layout: {{
        background: {{ color: '#111827' }},
        textColor: '#888',
    }},
    grid: {{
        vertLines: {{ color: '#1e2a3a' }},
        horzLines: {{ color: '#1e2a3a' }},
    }},
    crosshair: {{
        mode: LightweightCharts.CrosshairMode.Normal,
    }},
    rightPriceScale: {{
        borderColor: '#1e2a3a',
    }},
    timeScale: {{
        borderColor: '#1e2a3a',
        timeVisible: false,
    }},
}});

const candleSeries = chart.addCandlestickSeries({{
    upColor: '#00d4aa',
    downColor: '#ef4444',
    borderUpColor: '#00d4aa',
    borderDownColor: '#ef4444',
    wickUpColor: '#00d4aa',
    wickDownColor: '#ef4444',
}});

const smaSeries = chart.addLineSeries({{
    color: '#f59e0b',
    lineWidth: 2,
    title: 'SMA 200',
}});

// Load initial data
const initialCandles = {initial_candles_json};
const initialSma = {initial_sma_json};
const initialMarkers = {markers_json};

candleSeries.setData(initialCandles);
smaSeries.setData(initialSma);
if (initialMarkers.length > 0) candleSeries.setMarkers(initialMarkers);
chart.timeScale().fitContent();

if (initialCandles.length > 0) {{
    const last = initialCandles[initialCandles.length - 1];
    document.getElementById('chart-price').textContent = last.close;
}}

// Resize
window.addEventListener('resize', () => {{
    chart.applyOptions({{ width: chartEl.clientWidth }});
}});

// Chart switching
function showChart(symbol) {{
    document.getElementById('chart-symbol').textContent = symbol;
    document.querySelectorAll('.chart-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.chart-tab').forEach(t => {{
        if (t.textContent === symbol) t.classList.add('active');
    }});

    fetch('/api/chart?symbol=' + symbol)
        .then(r => r.json())
        .then(data => {{
            candleSeries.setData(data.candles || []);
            smaSeries.setData(data.sma || []);
            chart.timeScale().fitContent();
            if (data.candles && data.candles.length > 0) {{
                const last = data.candles[data.candles.length - 1];
                document.getElementById('chart-price').textContent = last.close;
            }}
            // Add entry markers for this symbol
            fetch('/api/markers?symbol=' + symbol)
                .then(r => r.json())
                .then(markers => {{
                    if (markers.length > 0) candleSeries.setMarkers(markers);
                    else candleSeries.setMarkers([]);
                }});
        }});
}}
</script>

</body>
</html>"""

    return html


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/chart":
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", ["EURUSD"])[0]
            data = get_chart_data(symbol, days=120)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif parsed.path == "/api/markers":
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            conn = init_db()
            trades = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open' AND symbol = ?", (symbol,)
            ).fetchall()
            markers = []
            for t in trades:
                t = dict(t)
                markers.append({
                    "time": str(t.get("opened_at", ""))[:10],
                    "position": "belowBar",
                    "color": "#00d4aa",
                    "shape": "arrowUp",
                    "text": f"LONG @ {t['entry_price']}",
                })
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(markers).encode())

        else:
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html = build_html()
            self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass


def main():
    port = 8050
    server = HTTPServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
