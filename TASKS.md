# Trading System — Task List

> **Last updated: 2026-07-26**
> Read this FIRST every session. This is the single source of truth for what's done, what's open, and what happened last.

---

## Last Session Recap (2026-07-26)

### What was built/fixed:
- **FX Bot (10 improvements):** TP broker sync, short entries, 3x daily schedule, scale-out at 1R (50% close + break-even stop), per-pair ADX regime, Telegram perf alerts, fee tracking (swap costs, 0.70% conversion fee, spread-adjusted sizing)
- **FX Bot PA strategy killed:** Strategy 101 backtested at Sharpe -1.08. Bugs fixed (center=True lookahead, bear_score gate, doji) but still unprofitable. Status set to "killed" in DB.
- **Weather Bot:** Per-side Kelly sizing (YES: 0.06, NO: 0.12), wired max_position_usd ($12 cap), raised min_edge_yes to 0.15
- **Stock Bot exit fix:** Fill verification, reconciliation (force-close after 2+ failed cuts), Telegram escalation for manual intervention
- **Stock Bot new strategies:** Asset Class TF (strategy 18, 5 ETFs, SMA-200) and Sector Momentum (strategy 5, top 3 of 10 SPDR sectors). Both have ownership ledgers to prevent cross-strategy conflicts. GitHub Actions workflows fire 1st trading day of month.
- **Dashboard:** Summary cards, per-bot P&L, inline SVG equity curve, risk section, dark theme

### Current state:
- FX Bot: 3 open trades (USDCHF, USDJPY, GBPJPY) on strategy 100, running 3x daily
- Stock Bot: holding AAPL, AMZN, GOOGL, QQQ, SPY, XLE. Churn loop fix deployed (May). Exit fix deployed (Jul 26).
- Weather Bot: running with per-side Kelly fix
- Events Bot: running every 30 min, batched LLM + Gemini search live
- New strategies: Asset Class TF + Sector Momentum ready, first run Aug 1

### Bugs found from Jul 27 pipeline run:
- [x] PA strategy 101 still generating signals despite being killed — fixed, pushed
- [x] PnL inflated 1000x — DB stores raw units but calc multiplied by 1000 again. GBPJPY corrected from $3,314 to $7.09
- [x] FX_ACCOUNT_BALANCE defaults to $10,000 but real demo is ~$287 — fixed: now reads live equity from broker at runtime.

### Checks to run next session (verify 2026-07-26 changes):
- [ ] FX Bot: scale-out at 1R — did any trade hit 1R and trigger 50% close + break-even stop?
- [ ] FX Bot: swap cost tracking — check a live trade's calculated swap vs Capital.com dashboard
- [ ] FX Bot: conversion fee deducted in P&L calc — compare calculated vs actual broker P&L
- [ ] FX Bot: strategy 101 not generating signals anymore (killed in DB)
- [ ] FX Bot: 3x daily schedule (07:00, 13:00, 22:00 UTC) — check GitHub Actions run history
- [ ] FX Bot: shorts — did any pair below SMA-200 generate a short entry?
- [ ] Stock Bot: exit reconciliation — any "ATTEMPTED CUT — FILL FAILED" entries in trade log? Did force-close trigger?
- [ ] Stock Bot: XLF/XLI churn stopped? No more buy-cut-buy-cut cycles?
- [ ] Stock Bot: realized P&L trend — still bleeding or stabilizing after exit fix?
- [ ] Weather Bot: bankroll reset to $100 clean slate (Jul 26). First post-fix trades should appear within hours.
- [ ] Weather Bot: YES-side win rate improving with min_edge 0.15 and Kelly 0.06?
- [ ] Weather Bot: max_position_usd ($12) actually capping trade sizes?
- [ ] Weather Bot: compare post-fix P&L vs pre-fix baseline (-$117 on 430 trades, 62% WR)
- [ ] Asset Class TF: Aug 1 first run — workflow triggers, script runs, state file committed?
- [ ] Sector Momentum: Aug 1 first run — workflow triggers, rankings computed, orders placed?
- [ ] Dashboard: accessible and showing correct data for all bots?

### Decision dates — judge if changes are positive or negative:
- **Aug 1:** Weather Bot — compare YES WR + P&L vs pre-fix. If still losing on YES, revert or tighten edge floor further.
- **Aug 1:** Stock Bot — is realized P&L stabilizing or still bleeding? If still losing, dig deeper into exit logic.
- **Aug 1:** Asset Class TF + Sector Momentum — first run. Just verify execution, too early to judge performance.
- **Aug 15:** FX Bot — 5-10 completed trades. Are shorts profitable? Did scale-out improve risk-adjusted returns? Are fee calcs matching Capital.com?
- **Nov 1:** Asset Class TF + Sector Momentum — 3 monthly rebalances done. Compare actual Sharpe/drawdown vs backtest (target: combined Sharpe ~2.12, MaxDD -6.5%).

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
