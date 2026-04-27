# Trading Admin

Master orchestrator for 3 trading bots. Aggregates stats, sends unified Telegram reports, and runs risk/regime/performance monitoring agents.

## Architecture

```
Trading Admin (this repo)
├── Agents
│   ├── Regime Detector — VIX + ADX → TRENDING/RANGING/VOLATILE/CRISIS
│   ├── Admin Risk — cross-bot exposure, correlation, drawdown kill switch
│   └── Performance Monitor — rolling Sharpe, decay detection, live vs backtest
│
├── Stock Bot (souhail123456/trading-bot)
│   ├── US stocks on Alpaca ($100k paper)
│   ├── Trades stored in memory/TRADE-LOG.md (SUMMARY block)
│   └── Runs on Railway
│
├── FX Bot (built-in)
│   ├── Capital.com ($1k demo)
│   ├── Strategies: Trend-Following (ID 100), Price Action (ID 101)
│   └── Trades stored in pipeline.db
│
└── Polymarket Bot (souhail123456/polymarket-bot)
    ├── EV Bot — prediction markets (Groq/Llama 3.3 70B)
    ├── Weather Bot — temperature markets
    └── Trades stored in logs/trades.jsonl + weather_trades.jsonl
```

## Workflows

| Workflow | Schedule | Description |
|----------|----------|-------------|
| `daily_report.yml` | 21:00 UTC Mon-Fri | Unified Telegram report (all 3 bots) |
| `daily_pipeline.yml` | 21:30 UTC Mon-Fri | FX signal generation + execution |

## Setup

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `BOT_GITHUB_TOKEN` | Fine-grained PAT (contents + actions read on all 3 repos) |
| `TELEGRAM_BOT_TOKEN` | @TradingAdmin_togo_bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `CAPITAL_API_KEY` | Capital.com API key |
| `CAPITAL_EMAIL` | Capital.com email |
| `CAPITAL_PASSWORD` | Capital.com password |

### Local Testing

```bash
# FX-only (no GitHub token needed)
cd src && python main.py --fx-only

# Full report
export GH_TOKEN="github_pat_..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
cd src && python main.py
```
