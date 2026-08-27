# Trading System — Task List

> **Last updated: 2026-08-27**
> Read this FIRST every session. This is the single source of truth for what's done, what's open, and what happened last.

---

## Last Session Recap (2026-08-26 / 27)

### ✅ S&P 500 index-trend strategy LIVE as daily dry-run (Milestone 1) — `bc3416c`
The strategic pivot below moved from idea → running code. New `pipeline/agents/index_pipeline.py` reuses the existing SMA-200 + MACD engine on the S&P 500 (yfinance `^GSPC` / Capital.com epic `US500`), **strategy_id 200**, fully separate from FX (100/101). Config locked to the winning backtest: **long-only, entry = close>SMA-200 AND MACD histogram>0, exit = SMA-200 re-cross ("let it run", no fixed TP), 2% risk over 2×ATR(14) stop, sizing in index points.**
- **Dry-run only** — logs the daily signal to `signals` (and marks `[DRY-RUN INTENT]` rows in `paper_trades` if it would enter/exit), **submits NO broker orders**. Runs fully offline (public yfinance, no `CAPITAL_*` secrets).
- Wired into `daily_pipeline.yml` as an offline step right after the FX step, so signals commit to `shared/pipeline.db` automatically each run (3×/weekday).
- **Verified on GitHub Actions** (run 33025609838, success): fetched 755 bars, computed **SIGNAL=FLAT** (S&P +7.97% above SMA-200 but MACD histogram −14.3 → momentum negative, correctly waits), logged strategy_id=200, no order. FX pipeline untouched (imports clean).
- **Next → Milestone 2** (after ~1 week of eyeballing signals vs chart): (1) confirm the `US500` demo contract point value (assumed **$1/point**); (2) index-specific execution in `broker_capital.py` (point-based stop, no TP, SMA-recross exit monitor); (3) real `paper_trades` open rows + `broker_order_id`; (4) a `--live` flag guard (currently `run(dry_run=False)` raises `NotImplementedError`); (5) daily Actions step in live mode with secrets.

### 🔬 TradingView full-history research — why gold was a mirage, S&P is the pick
Ran the SMA-200+MACD Pine strategy (`tradingview/gold_trend_sma_macd.pine`, now with date-window input + alert_message payloads) across instruments, long-only, "let it run" (TP=100). **Central lesson: every spectacular number regressed toward PF ~1.5 / ~30% DD once tested over long/harsh history — the moonshots were period artifacts.**
- **Fair apples-to-apples on the modern window (~1990+):** **S&P PF 3.29 / +136% / 7.1% DD / 102 trades (29 winners) = WINNER.** Gold PF 2.44 / +89% / 24% DD but only 19 winners (fragile). Nasdaq PF 4.45 but only +20% / 18 trades / **4 winners** (statistically meaningless — barely trades because it trends too smoothly to re-cross the SMA). Gold full-history (43yr) PF **1.03** = no edge; its +880%/PF1.71 was a cherry-picked bull run. Intraday VWAP scalping PF 0.587 = loses. Shorts hurt on every instrument.
- **Why S&P flipped from "mediocre" to "best":** the 155-yr test (PF 1.49 / 35% DD) was punishing it with 1871–1990 reconstructed/illiquid data from a market that no longer exists. On the relevant modern window it's genuinely strong.
- **Caveats to respect:** 29 winners = promising not proven; Pine ignores swap/overnight financing (real returns lower) → the demo forward-test is what proves it.

### 📊 "Why can't I see results?" — resolved
FX bot is running fine (last signal Aug 26; 5 open demo positions, 29 closed, net **−$13** ≈ flat — exactly as FX backtested = the weakest strategy). Two real reasons results felt invisible: (1) local repo was 18 commits behind origin (bot commits daily, laptop wasn't pulling); (2) the bot was trading the **weak FX strategy**, not the strong S&P one. Milestone 1 above fixes #2's root: the good strategy now runs (dry-run) and will produce watchable results.

---

## Last Session Recap (2026-08-20 / 21)

### 🧭 STRATEGIC PIVOT under consideration — trading the wrong universe (2026-08-21):
Backtested the live SMA-200 + MACD trend engine on gold/indices/oil vs the 10 FX pairs (`fx_backtester.py --universe`, backtest-only, live universe untouched, `91159b4`). Result: **non-FX beats FX by ~+55% risk-adjusted.**
- Gold **1.49 Sharpe / -3.6% DD / 18.7% CAGR** (best return-per-drawdown), Nasdaq 1.49 (/-7.4%), S&P 1.32 (/-4.0%). FX average **0.90 Sharpe**; 9 of 10 pairs below every metal/index. Best FX = NZD/USD 1.17. Oil/silver high return but -17%/-21% DD.
- **Why it matters:** the trend engine is fine — it was pointed at the most efficient (hardest-to-trend) markets. FX majors are range-bound by design.
- **Convergence insight:** gold/indices are SINGLE instruments → they sidestep the cross-sectional top-3 ranking that tied FX to the custom stack → map cleanly to ONE TradingView Pine script. Available on Capital.com and every TV-integrated broker.
- **Proposed direction (discussed, not yet built):** rebuild as a gold (or gold+Nasdaq) SMA-200+MACD trend strategy as one Pine script on TradingView, on a broker demo, left to prove itself — kills the entire custom pipeline + reconciliation bug class. Watch fees/swap: TV built-in paper ignores spread & swap (overstates FX-style results); prefer a broker-demo integration (e.g. OANDA demo) for realistic costs. Alpaca IS a native TV broker (stock/ETF side); Capital.com is NOT a TV execution broker (it only embeds TV charts).
- **Bigger frustration acknowledged:** ~4 months, flat P&L. Root causes: (1) most effort went to infrastructure/bug-fixing, and P&L was mislabeled/untrustworthy until the true-fill fix landed 08-21 — the honest evaluation clock effectively starts now; (2) breadth over depth across 6 bots on conventional/efficient-market strategies. Recommendation: stop building, park losers, let the two defensible things (ETF strategy + now-accurate FX/gold) run 2-3 weeks and DECIDE on real numbers.

### ✅ Four fixes shipped + verified on GitHub Actions:

**LLM router fixed** (trading-bot `92bf67a`) — root cause: Groq retired `llama-3.1-8b-instant` Aug 16 (scans died Aug 17), Gemini retired `gemini-2.0-flash` June 1. New IDs: Gemini `gemini-2.5-flash`/`-lite`, Groq `openai/gpt-oss-20b`/`-120b`, Cerebras `llama-3.3-70b`/`llama3.1-8b`, OpenRouter `gpt-oss-20b:free`/`qwen3-coder:free`. Also fixed the 404→fallback bug (dead primary now falls through instead of killing the provider) and updated `news_sentiment.py`. Live Midday Scan passed: `LLM OK — gemini/gemini-2.5-flash`.

**FX reconciliation fixed** (trading-admin `2a8d0be`) — root cause: reverse-sync in `fx_pipeline.py` closed any DB position absent from a SINGLE `get_positions()` call; Capital.com returns transient partial/empty lists, so healthy 3R runners got false-closed at noise prices then re-imported as fresh trades (resetting the clock). Fix: require **N=2 consecutive misses** before closing (counter resets on reappear), transient-empty guard (never mass-close when broker returns zero), defensive SYNC logging (broker epics vs DB symbols), and 429 exponential-backoff retry on session auth (`broker_capital.py`). Live pipeline run green, broker/DB matched 2/2, no false closes.
- **Correction:** the broker truly holds only **2 open FX positions (GBPJPY, USDJPY)**, not 5. The committed `shared/pipeline.db` (5 open) is stale vs the accurate CI `data/pipeline.db` (2 open) — the known "two divergent DBs" issue. The `broker_stop_orphan_cleanup` rows were a one-time Jul-31 migration event, not ongoing.
- **Remaining confirmation item:** positions closed by the broker's own server-side TP/SL are still labeled `broker_sync_missing` at a wrong current-price P&L. Proper fix = read Capital.com activity/history endpoint for the true fill (not done — needs live API testing).

**Divergent-DBs bug fixed** (trading-admin `5cd75b5`) — root cause: pipeline DB runs in WAL journal mode but `run_daily()` never closes/checkpoints, so committed rows sat in the `-wal` sidecar. The workflow did an early `cp data/pipeline.db shared/` that copied only the pre-checkpoint main file, while the later cache-save captured the post-checkpoint file → `shared/` (git) froze ≥1 transaction behind the accurate CI cache. Fix: removed the early cp; added one WAL-safe export step running **last + `if: always()`** that does `PRAGMA wal_checkpoint(TRUNCATE)` then copies to `shared/` and clears sidecars. Now `data/pipeline.db` is the single source of truth and `shared/` is always a faithful export. Live run (32324819510) green: broker == data == shared, all 3 agree.
- **Corrected position count:** broker now genuinely holds **4 open FX** — GBPJPY, USDJPY, plus **USDCHF + EURGBP opened today (Aug 20)**. My earlier "2 vs 5" was the divergence itself; the truth moved to 4 and all sources now match. No rows hand-edited (no trade-history risk).

### Original status review (2 live issues found):

**🔴 Stock Bot discretionary scans DOWN since Aug 17** — all 3 LLM providers 404:
- `gemini-2.0-flash`, `groq/llama-3.1-8b-instant`, `cerebras/llama3.1-70b` all return HTTP 404 (deprecated free-tier model IDs got rotated). Router in `scripts/llm_router.py`.
- Impact: Pre-market Research, Market Open, Midday/Afternoon Scan, Daily Summary all fail. Last success Aug 14.
- **NOT impacted:** mechanical Strategy 18 (Asset Class TF) doesn't call the LLM → still runs, positions safe.
- Secondary bug: `call_llm` only falls back to `fallback_model` on rate-limit/payload errors, NOT on 404 (llm_router.py ~line 313). A dead primary model kills the provider instead of trying the fallback.

**🟠 FX Bot broker reconciliation churn** — 17 of 23 closed trades exited via broker sync, not strategy:
- 10× `broker_sync_missing`, 7× `broker_stop_orphan_cleanup`, only 6 clean TP/SL exits.
- Positions keep vanishing/orphaning on Capital.com → trades close early instead of running to 3R TP. This is likely why realized PnL is flat/negative.
- One transient failure Aug 19 22:31: Capital.com `429 too-many-requests` on /session (no retry/backoff on auth). Isolated — other runs that day succeeded.

### Current state (Aug 20):
- **FX Bot:** 5 open longs — USDCHF (Jul 31), USDCAD (Aug 7), GBPUSD (Aug 10), GBPJPY (Aug 11), USDJPY (Aug 12). 23 closed, realized **-$6.30**. Regime TRENDING, VIX 15.2. Running 3x/day (1 transient 429 fail).
- **Stock Bot:** equity **$100,159** (Aug 16), unrealized +$2,761. Holding 6 ETFs steady (DBC, EFA, SPY, VNQ, XLF, XLK) — **churn confirmed stopped**. Recovered above $100k start. Discretionary scans down (see above); ETF strategy fine.
- **Weather Bot:** active (last trade today). 183 trades, 169 resolved, **59.8% WR but realized -$0.75** (balance $100.4→$101.04). High hit rate not converting — NO bets at 0.42-0.58 entry yield tiny wins. Almost all NO (179/4).
- **Events (EV) Bot:** still paused. Commit c26e3ad tightened params / blocked YES / added skepticism penalty but no trades placed.

### Carry-forward items now RESOLVED:
- [x] Stock Bot churn stopped — 6 ETFs held steady weeks, no buy/sell thrash
- [x] Stock Bot realized P&L recovered — equity above $100k start
- [x] Asset Class TF + Sector Momentum first runs — both executed, state files committed
- [x] Weather Bot post-fix trades resolving — 169 resolved, 59.8% WR
- [x] FX EURGBP short — first short closed +$1.60

### FX TA entry filters — MACD added, RSI rejected (trading-admin `747b761`, 2026-08-20):
- Added two configurable entry filters to `fx_trend_signals()` (entry candidates only; exit + risk layer untouched): **MACD histogram agreement** and **RSI-14 exhaustion guard**. Toggles/thresholds via params (`macd_filter`, `rsi_filter`, `rsi_overbought`=70, `rsi_oversold`=30).
- **Backtest gate** (2006–2026, 10 pairs, long-only monthly approx, net 3bps): Baseline SMA-200 Sharpe 2.28 → **+MACD 2.96** (win 80.6%, DD -3.2%, 536 trades) → +RSI 1.69 (worse) → +both 2.36.
- **Decision:** `MACD_FILTER_DEFAULT=True` (clear Sharpe lift), `RSI_FILTER_DEFAULT=False` (hurt Sharpe + doubled DD standalone; kept in code, re-enable via `rsi_filter=True`).
- **⚠️ Effectively live next run** — MACD is on main by default; next scheduled FX pipeline uses it.
- **Caveat / to verify:** backtester is long-only monthly; the SHORT-side filter is logic-verified only, not backtested. Watch the first few MACD-gated short entries.

### FX true-fill labeling — DONE + live-verified (trading-admin `71cec53`→`a5a2722`, 2026-08-21):
- Confirmed-gone closes now read the broker's REAL fill history first (exit price, close time, realized P&L, truthful reason `broker_stop`/`broker_tp`/`broker_closed`). Old current-price `broker_sync_missing` estimate is now only a fallback when history returns None.
- New `broker_capital.py: get_deal_history(deal_id, symbol, since_hours)` + read-only `--deal-history <id>` CLI flag.
- **Real schema (verified live — Capital.com docs were WRONG):** close = `type:"POSITION"` activity keyed by `source` (SL/TP/CLOSE_OUT), fill in `details.level`, time `dateUTC`; realized cash P&L from `history/transactions` `size` field by `dealId` (no `profitAndLoss`/`closeLevel` fields exist live). Both endpoints cap spans at ~24h → queries chunked. `_implied_exit_price()` derives exit when only P&L is available.
- **Proof of the old bug:** GBPUSD real +$10.69 vs recorded +$4.89; USDCHF real -$13.19 vs -$14.29.
- Pipeline green (32507779399); AUDUSD already mid-flight on the fixed path. Orphan path (`reconcile_broker_vs_db`) books no price/pnl so left unchanged.

### Still open / carry forward:
- **Align cache keys across workflows** — `weekly_research.yml` caches `data/pipeline.db` under a non-`v62` key but its `restore-keys: pipeline-db-` prefix can pull `daily_pipeline`'s `v62` cache; asymmetric lineage could reintroduce drift if weekly ever writes back.
- **`v62` cache-miss re-seed vector** — a cache miss still seeds a fresh DB (historical strategy-101 re-seed cause); currently guarded by `KILLED_STRATEGIES` enforcement in `db.py`, but the seed-on-miss path remains.
- **Monitor first LLM-router run in a live scan window** — Midday Scan passed via Gemini; confirm daily scans resume producing research/summaries.
- **Weather Bot** — dropped this session (flat $100 paper balance, not worth the effort per user).

---

## Prior Session Recap (2026-07-28)

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
- [x] FX deal ID orphan bug fixed (Capital.com confirm vs position endpoint mismatch) — 2026-07-29
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
