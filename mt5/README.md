# TrendEA — MetaTrader 5 Expert Advisor

A **long-only, Daily-timeframe** trend strategy. Ride the trend while price
stays above the 200-SMA with positive MACD momentum (and, for gold, a minimum
ADX trend strength). Exit on the SMA recross — no fixed take-profit, we let
winners run. A 2×ATR protective stop is attached purely as a safety net.

## Strategy in one line

- **Entry (long):** Close > SMA(200) AND MACD histogram > 0 AND (if `UseADX`) ADX(14) > `AdxMin`
- **Exit:** Close < SMA(200) — the SMA-recross exit (no take-profit)
- **Protective stop:** hard SL at 2×ATR(14) below entry (safety only)
- **Sizing:** risk `RiskPct`% of equity across the stop distance, converted to
  lots from the symbol's real tick value/size at runtime

All conditions are evaluated on the **last closed daily bar**, once per new bar.

## 1. Install

1. In MetaTrader 5: **File → Open Data Folder**.
2. Navigate to `MQL5/Experts/`.
3. Copy `TrendEA.mq5` into that folder.
4. Open **MetaEditor** (or double-click the file), select `TrendEA.mq5`, and
   press **F7** to compile. You should get `0 errors, 0 warnings` and a
   `TrendEA.ex5` next to the source.
5. Back in the terminal, refresh the **Navigator** (right-click → Refresh) and
   `TrendEA` appears under **Expert Advisors**.

## 2. Enable algo trading

- Click the **Algo Trading** toolbar button so it is green/enabled.
- When attaching, on the **Common** tab tick **Allow Algo Trading**.
- (Live trading only) ensure the account allows automated trading.

## 3. Attach to a D1 chart

1. Open a **Daily (D1)** chart of your target symbol.
2. Drag `TrendEA` from the Navigator onto the chart.
3. Set the inputs (see per-instrument table below) and confirm.
4. A smiley face in the top-right of the chart = EA is running.

> **Timeframe matters.** The strategy is validated on **D1**. The EA reads
> `_Period`, so it technically runs on any timeframe you attach it to — but only
> attach it to **Daily** charts.

## 4. Validate in the Strategy Tester

1. **View → Strategy Tester** (Ctrl+R).
2. Expert: `TrendEA`. Symbol: e.g. `XAUUSD`. Timeframe: **D1**.
3. Modelling: **Every tick based on real ticks** (or "1 minute OHLC" for a fast
   first pass). Set a date range with plenty of history.
4. On the **Inputs** tab set the per-instrument values below.
5. Run. Check the **Graph**, **Report**, and **Journal** tabs. The Journal
   prints entry/exit lines and any "risk slightly higher than target" warnings
   when the min-lot floor is hit.

## 5. Per-instrument input settings

The only setting that changes between instruments is **`UseADX`**.

| Instrument            | Symbol (broker-dependent) | `UseADX` | Notes                                  |
|-----------------------|---------------------------|----------|----------------------------------------|
| Gold                  | `XAUUSD`                  | `true`   | ADX(14) > 25 trend-strength gate       |
| S&P 500 index         | `US500` / `SPX500`        | `false`  | No ADX gate                            |
| Nasdaq 100 index      | `NAS100` / `USTEC`        | `false`  | No ADX gate                            |
| Dow 30 index          | `US30` / `DJ30`           | `false`  | No ADX gate                            |

Common defaults (leave as-is unless testing):

| Input          | Default     |
|----------------|-------------|
| `SmaLen`       | 200         |
| `MacdFast`     | 12          |
| `MacdSlow`     | 26          |
| `MacdSignal`   | 9           |
| `AdxLen`       | 14          |
| `AdxMin`       | 25.0        |
| `AtrLen`       | 14          |
| `AtrStopMult`  | 2.0         |
| `RiskPct`      | 2.0         |
| `MagicNumber`  | 20260827    |
| `TradeComment` | `TrendEA`   |

### Notes

- **One position per symbol.** The EA manages only its own trades (matched by
  `MagicNumber`), so you can run it on several charts/symbols at once — give
  each a **distinct `MagicNumber`** if you want them fully independent.
- **Sizing is symbol-aware.** Lots are computed from
  `SYMBOL_TRADE_TICK_VALUE` / `SYMBOL_TRADE_TICK_SIZE`, so the same EA sizes
  correctly across XAUUSD, US500, NAS100, etc. If the risk-based lot falls below
  the broker's minimum volume, the EA uses the minimum and prints a warning that
  actual risk is slightly above `RiskPct`.
- **Verify your broker's symbol name** (e.g. `XAUUSD` vs `GOLD`, `US500` vs
  `SPX500`) — set the symbol on the chart accordingly.
