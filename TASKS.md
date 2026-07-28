# Trading System — Task List

> **Last updated: 2026-07-28**
> Read this FIRST every session. This is the single source of truth for what's done, what's open, and what happened last.

---

## Last Session Recap (2026-07-28)

### What was built/fixed:
- **PA strategy 101 permanently killed** (3-layer fix): hardcoded KILLED_STRATEGIES dict in db.py enforced on every init, code-level block in signal generator, regime detector marks killed strategies. Root cause: no code path ever set status='killed', two divergent DBs (data/ vs shared/), seed function overwrote status on every run.
- **FX PnL inflation fixed**: paper_executor.py used raw `(exit-entry)*qty` for all pairs — for JPY crosses this gives PnL in JPY (~150x inflated). Added `_calc_pnl()` helper that routes FX pairs through `calculate_fx_pnl`. Trade 8 corrected: $3,314 → $6.90. Total realized: $10.63.
- **Weather bot bankroll poison fixed**: GitHub Actions cache was restoring old pre-reset trades (-$117) over the clean bankroll, keeping it at -$17 (all Kelly sizes = $0). Isolated cache namespaces per bot (weather-logs-v2-, ev-logs-v2-, etc.).
- **8 orphaned trades cleaned** from pipeline.db (duplicate qty=1 entries from early runs).

### Current state (Jul 28):
- FX Bot: 3 open trades — USDJPY long @161.756 (Jul 10), USDCHF long @0.81869 (Jul 27), GBPJPY long @217.779 (Jul 28). Realized P&L: $10.63 (3 closed). Regime: TRENDING (ADX 25.2).
- Stock Bot: equity $100,284. Holding AAPL (+$1,384), SPY (+$97), XLE (+$1,201), XLF (+$315), XLI (-$71). Unrealized: +$2,926. No churn on XLF/XLI.
- Weather Bot: was silenced by cache poison since Jul 26. Fix pushed — should start placing trades on next run.
- Events Bot: paused (intentional, 10% WR)
- New strategies: Asset Class TF + Sector Momentum ready. Aug 1 is Saturday → first run Mon Aug 3.

### Verified (Jul 28):
- [x] FX Bot: 3x daily schedule — 07:00, 13:00, 22:00 UTC confirmed in workflow YAML. Runs at 09:36/15:20 are normal GHA delay. 22:00 run should fire tonight.
- [x] FX Bot: PA strategy 101 — permanently killed with 3-layer defense (code + DB + regime)
- [x] FX Bot: PnL formula — fixed for all FX pairs (JPY cross conversion)
- [x] FX Bot: orphaned trades — 8 deleted, 6 clean trades remain
- [x] Asset Class TF workflow — verified, cron fires days 1-3 Mon-Fri with Alpaca calendar guard
- [x] Sector Momentum workflow — verified, cron fires days 1-7 with Alpaca calendar guard
- [x] Weather Bot cache poison — fixed, isolated cache namespaces per bot
- [x] Weather Bot filters loosened — min_edge 12%→8%, min_no_entry $0.50→$0.35, timeout 15→30s. First run: 6 trades placed.
- [x] FX Bot: max positions 3→5, take-profit at 3R, max hold 90→30 days. First run: USDJPY closed at 3R TP (+$9.77), 2 new entries (USDJPY long, EURGBP short — first short ever). Realized $20.40.
- [x] Stock Bot anti-churn overhaul: killed SMA-20 time-based cut, 3% trend strength + positive momentum entry filter, auto profit-taking at +15%/+25%, removed XLI/XLV from universe, 3-day position age protection.
- [x] .gitignore: added *.db-shm, *.db-wal, trading.db to both repos

### Still to verify (carry forward):
- [ ] FX Bot: swap cost tracking — no closed long-held trade yet with swap breakdown
- [ ] FX Bot: conversion fee — no closed non-USD trade with fee breakdown visible yet
- [ ] FX Bot: EURGBP short — first short trade, monitor performance
- [ ] Stock Bot: churn actually stopped? Monitor next week's buy/sell ratio
- [ ] Stock Bot: profit-taking triggering? AAPL is +4.5%, watch for +15% half-take
- [ ] Stock Bot: realized P&L trend — still -$2,618, should stabilize now
- [ ] Weather Bot: 6 trades placed — check resolution, WR, P&L
- [ ] Weather Bot: YES-side win rate with min_edge 0.15?
- [ ] Asset Class TF: Aug 3 first run — workflow fires, script runs, state committed?
- [ ] Sector Momentum: Aug 3 first run — rankings computed, orders placed?
- [ ] Dashboard: accessible and showing correct data?

### Decision dates:
- **Aug 3:** Asset Class TF + Sector Momentum — first run (Aug 1 is Saturday). Verify execution only.
- **Aug 5:** Weather Bot — first post-fix trades resolving. WR + P&L trend.
- **Aug 5:** Stock Bot — has churn stopped? Buy/sell ratio closer to 1:1?
- **Aug 15:** FX Bot — 5-10 completed trades. 3R TP working? Shorts profitable? Fees matching?
- **Aug 15:** Stock Bot — realized P&L recovering from -$2,618?
- **Nov 1:** Asset Class TF + Sector Momentum — 3 rebalances done. Compare vs backtest (target Sharpe ~2.12, MaxDD -6.5%).

### USER ACTION NEEDED:
- [ ] **Create Telegram channel** for centralized bot logs — name it "Trading Bot Logs", add @TradingAdmin_togo_bot as admin. Give the channel name/ID to Claude so all 3 bots get wired to post there.

### What's NOT done (carry forward):
- Wire all 3 bots to post to Telegram channel (blocked on user creating it)
- Events Bot: resolution scraping, cross-market arbitrage, time decay sniper
- FX capital allocation: best strategy (Sharpe 2.62) on $1k demo — needs scale-up plan

---

## High Priority

### Events Bot — Edge Features
- [ ] Resolution source scraping — check actual data before market resolves
- [ ] Cross-market arbitrage — detect mispriced related markets
- [ ] Time decay sniper — bet status quo aggressively near expiry
- [ ] Pre-filter obvious markets with math before LLM

### FX Bot — Scale Up
- [ ] Plan transition from $1k demo to real/larger account (strategy 100 has Sharpe 2.62)

---

## Medium Priority

### Stock Bot
- [ ] Monitor XLF/XLI churn fix (cooldown deployed May, should be stable)
- [ ] Track realized P&L trend — was -$2,618, exit fix should stop bleeding

### Dashboard
- [ ] Total portfolio value across ALL platforms (Polymarket + Alpaca + Capital.com)
- [ ] P&L chart over time per bot (cumulative line chart)
- [ ] Edge accuracy check (LLM claimed edge vs actual win rate)

### All Bots
- [ ] Combined portfolio target: Sharpe ~2.12, MaxDD -6.5% (vs old -45.2%)

---

## Paused Bots — intentionally stopped (losing money, no real strategy)
- **Events Bot (EV):** 10% WR, -$117. Stopped May 8. Needs proper strategy before restarting — resolution scraping, arbitrage, time decay are the path forward.
- **Econ Bot:** 0% WR, -$15. Stopped May 19. 10 pending trades never resolved. Needs strategy rethink.
- **Crypto Bot:** $0, no trades. Stopped May 14. No crypto price markets on Polymarket yet.
- **DO NOT re-enable these until mechanical strategies (FX, Asset Class TF, Sector Momentum) are proven profitable.** Prove first, then fix.

## Backlog
- [ ] Stock bot Telegram EOD fix (parse_groq_response.py)
- [ ] Real money deployment plan once paper strategies proven

---

## Done
- [x] FX Bot: 10 improvements (TP sync, shorts, 3x daily, scale-out, ADX, perf alerts) — 2026-07-26
- [x] FX Bot: fee tracking (swap, conversion, spread-adjusted sizing) — 2026-07-26
- [x] FX Bot: PA strategy 101 killed (Sharpe -1.08) — 2026-07-26
- [x] FX Bot: PA bugs fixed (center=True, bear_score gate, doji, hammer) — 2026-07-26
- [x] Weather Bot: per-side Kelly (YES 0.06, NO 0.12), max_position_usd wired — 2026-07-26
- [x] Stock Bot: exit execution fix (fill verify, reconciliation, escalation) — 2026-07-26
- [x] Stock Bot: Asset Class TF (strategy 18) with ownership ledger — 2026-07-26
- [x] Stock Bot: Sector Momentum (strategy 5) with ownership ledger — 2026-07-26
- [x] Stock Bot: GitHub Actions workflows for both new strategies — 2026-07-26
- [x] Dashboard: upgraded with summary cards, equity curve, risk section — 2026-07-26
- [x] Backtests: FX strategies 100/101 results stored in shared/pipeline.db — 2026-07-26
- [x] PA strategy 101 permanently killed (3-layer: code + DB + regime) — 2026-07-28
- [x] FX PnL inflation fixed (JPY cross conversion in paper_executor) — 2026-07-28
- [x] Weather bot cache poison fixed (isolated cache namespaces per bot) — 2026-07-28
- [x] Orphaned trades cleaned (8 deleted from pipeline.db) — 2026-07-28
- [x] 3x daily FX schedule + Aug 1 workflows verified — 2026-07-28
- [x] Events Bot: bankroll floor + max 15 markets per scan — 2026-05-04
- [x] Events Bot: live crypto prices, market categories, LLM calibration — 2026-05-04
- [x] Events Bot: batch LLM calls (~80% token savings) — 2026-05-04
- [x] Events Bot: Gemini Search grounding — 2026-05-04
- [x] Stock Bot: momentum breakout signal, short-selling, 7 positions — 2026-05-04
- [x] Stock Bot: churn loop fix (trade log cooldown scan) — 2026-05-14
- [x] Stock Bot: exit bug fix (cuts array bypass) — 2026-05-14
- [x] Weather Bot: YES trades back, 5 ensemble models, city sizing — 2026-05-04
- [x] Econ Bot: country filter, wider sigmas — 2026-05-04
- [x] All repos: User-Agent fix, health check Sunday-only — 2026-05-04
