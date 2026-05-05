# Trading System — Task List

## Verify Results (from 2026-05-04 changes)
- [ ] Events Bot: confidence values varying (not flat 0.80)?
- [ ] Events Bot: crypto trades using live prices in reasoning?
- [ ] Events Bot: bankroll cap working (not draining below $2)?
- [ ] Events Bot: check resolved trades — win rate, P&L by category
- [ ] Stock Bot: did momentum breakout signal fire on any new stocks?
- [ ] Stock Bot: did any short signals fire?
- [ ] Stock Bot: MSFT still held or stopped out?
- [ ] Stock Bot: how many positions now (was 3, max is 7)?
- [ ] Weather Bot: YES trades performing? City sizing working?
- [ ] Econ Bot: country filter blocking non-US markets?

---

## High Priority

### Events Bot — Token Savings
- [x] Batch LLM calls (15 markets → 1 call, ~80% token savings)
- [x] Cache unchanged markets between scans (skip if price moved <3%)
- [ ] Pre-filter obvious markets with math before LLM

### Events Bot — Edge Features
- [x] Gemini Search grounding — let LLM Google in real-time
- [ ] Resolution source scraping — check actual data before market resolves
- [ ] Cross-market arbitrage — detect mispriced related markets
- [ ] Time decay sniper — bet status quo aggressively near expiry

### FX Bot
- [ ] Analyze trade data — what's winning/losing and WHY
- [ ] Improve strategy based on data

---

## Medium Priority

### Stock Bot
- [ ] Time-based cut rule (negative 5+ days + below SMA-20 → cut)
- [ ] Monitor momentum breakout + shorting performance

### All Bots
- [ ] Monitor all new changes over 1-2 weeks
- [ ] Dashboard updates for new features (categories, shorts)

---

## Unified Dashboard — All 6 Bots
- [ ] Add Stock Bot section (positions, P&L, open longs/shorts, momentum signals)
- [ ] Add FX Bot section (positions, P&L, open trades)
- [ ] P&L chart over time per bot (cumulative line chart, pure SVG)
- [ ] Bankroll/cash remaining per bot (not just P&L)
- [ ] Events Bot category breakdown (win rate: crypto vs sports vs general)
- [ ] Edge accuracy check (claimed edge vs actual win rate — is LLM calibrated?)
- [ ] City heat map for Weather Bot (green=profit, red=loss)
- [ ] Streak tracker per bot (current win/loss streak)
- [ ] Total portfolio value across ALL platforms (Polymarket + Alpaca + Capital.com)

## Backlog
- [ ] Stock bot Telegram EOD fix (parse_groq_response.py)
- [ ] Real money deployment plan once strategies proven
- [ ] Crypto bot — revisit when Polymarket adds crypto price markets

---

## Done
- [x] Events Bot: bankroll floor + max 15 markets per scan
- [x] Events Bot: live crypto prices, market categories, LLM calibration
- [x] Stock Bot: momentum breakout signal (20-day high + SMA-50)
- [x] Stock Bot: short-selling (death cross + momentum breakdown)
- [x] Stock Bot: 7 positions, 15% size, 5 trades/week
- [x] Weather Bot: YES trades back, 5 ensemble models, city sizing
- [x] Econ Bot: country filter, wider sigmas
- [x] All repos: User-Agent fix, health check Sunday-only
