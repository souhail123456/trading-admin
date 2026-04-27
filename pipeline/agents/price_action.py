"""
Price Action Pattern Detector
-----------------------------
Detects candlestick patterns and market structure on daily OHLCV data.
Returns a DataFrame of signals per ticker per date.

Patterns detected:
  Candlestick: bullish/bearish engulfing, pin bar (hammer/shooting star), doji
  Structure:   higher highs/lows (uptrend), lower highs/lows (downtrend),
               break of structure (BoS)
  Multi-TF:    weekly trend direction as higher-timeframe filter

Usage:
    from pipeline.agents.price_action import detect_all_patterns
    signals = detect_all_patterns(ohlcv_df)
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Candlestick patterns (daily)
# ---------------------------------------------------------------------------

def _body(df: pd.DataFrame) -> pd.Series:
    return df["close"] - df["open"]

def _body_abs(df: pd.DataFrame) -> pd.Series:
    return _body(df).abs()

def _upper_wick(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df[["open", "close"]].max(axis=1)

def _lower_wick(df: pd.DataFrame) -> pd.Series:
    return df[["open", "close"]].min(axis=1) - df["low"]

def _range(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]


def detect_engulfing(df: pd.DataFrame) -> pd.DataFrame:
    """
    Bullish engulfing: prev red candle, current green candle that engulfs prev body.
    Bearish engulfing: prev green candle, current red candle that engulfs prev body.
    """
    body = _body(df)
    prev_body = body.shift(1)
    ba = _body_abs(df)
    prev_ba = ba.shift(1)

    bullish = (
        (prev_body < 0) &                          # prev was red
        (body > 0) &                                # current is green
        (df["close"] > df["open"].shift(1)) &       # close above prev open
        (df["open"] < df["close"].shift(1)) &       # open below prev close
        (ba > prev_ba)                              # bigger body
    ).astype(int)

    bearish = (
        (prev_body > 0) &                           # prev was green
        (body < 0) &                                # current is red
        (df["open"] > df["close"].shift(1)) &       # open above prev close
        (df["close"] < df["open"].shift(1)) &       # close below prev open
        (ba > prev_ba)
    ).astype(int)

    return pd.DataFrame({
        "bullish_engulfing": bullish,
        "bearish_engulfing": bearish,
    }, index=df.index)


def detect_pin_bar(df: pd.DataFrame, wick_ratio: float = 2.0) -> pd.DataFrame:
    """
    Hammer (bullish pin bar): long lower wick >= wick_ratio * body, small upper wick.
    Shooting star (bearish pin bar): long upper wick >= wick_ratio * body, small lower wick.
    """
    ba = _body_abs(df)
    uw = _upper_wick(df)
    lw = _lower_wick(df)
    rng = _range(df)

    # Avoid division by zero
    min_range = rng.median() * 0.3
    valid = rng > min_range

    hammer = (
        valid &
        (lw >= wick_ratio * ba) &           # long lower wick
        (uw < ba * 0.5) &                   # small upper wick
        (ba > 0)                            # has a real body
    ).astype(int)

    shooting_star = (
        valid &
        (uw >= wick_ratio * ba) &           # long upper wick
        (lw < ba * 0.5) &                   # small lower wick
        (ba > 0)
    ).astype(int)

    return pd.DataFrame({
        "hammer": hammer,
        "shooting_star": shooting_star,
    }, index=df.index)


def detect_doji(df: pd.DataFrame, threshold: float = 0.1) -> pd.DataFrame:
    """
    Doji: body is very small relative to range (indecision).
    """
    ba = _body_abs(df)
    rng = _range(df)
    min_range = rng.median() * 0.3

    doji = (
        (rng > min_range) &
        (ba < rng * threshold)
    ).astype(int)

    return pd.DataFrame({"doji": doji}, index=df.index)


# ---------------------------------------------------------------------------
# Market structure (swing highs/lows, trend, break of structure)
# ---------------------------------------------------------------------------

def detect_swing_points(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """
    Swing high: high is highest in ±lookback window.
    Swing low: low is lowest in ±lookback window.
    """
    rolling_high = df["high"].rolling(2 * lookback + 1, center=True).max()
    rolling_low = df["low"].rolling(2 * lookback + 1, center=True).min()

    swing_high = (df["high"] == rolling_high).astype(int)
    swing_low = (df["low"] == rolling_low).astype(int)

    return pd.DataFrame({
        "swing_high": swing_high,
        "swing_low": swing_low,
    }, index=df.index)


def detect_structure(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """
    Market structure based on swing points:
    - Uptrend (HH + HL): higher highs and higher lows
    - Downtrend (LH + LL): lower highs and lower lows
    - Break of structure (BoS): trend reversal signal

    Returns: structure column with values:
      1 = bullish BoS (downtrend broken to upside)
     -1 = bearish BoS (uptrend broken to downside)
      0 = no signal
    """
    swings = detect_swing_points(df, lookback)

    # Extract swing high/low values
    sh_vals = df["high"].where(swings["swing_high"] == 1)
    sl_vals = df["low"].where(swings["swing_low"] == 1)

    # Forward-fill to get "last swing high/low"
    last_sh = sh_vals.ffill()
    prev_sh = sh_vals.ffill().shift(1)
    last_sl = sl_vals.ffill()
    prev_sl = sl_vals.ffill().shift(1)

    # Higher high / lower low detection
    hh = (last_sh > prev_sh) & swings["swing_high"].astype(bool)
    ll = (last_sl < prev_sl) & swings["swing_low"].astype(bool)
    lh = (last_sh < prev_sh) & swings["swing_high"].astype(bool)
    hl = (last_sl > prev_sl) & swings["swing_low"].astype(bool)

    # Simple BoS: close breaks above last swing high (bullish) or below last swing low (bearish)
    bullish_bos = (
        (df["close"] > last_sh.shift(1)) &
        (df["close"].shift(1) <= last_sh.shift(1))
    ).astype(int)

    bearish_bos = (
        (df["close"] < last_sl.shift(1)) &
        (df["close"].shift(1) >= last_sl.shift(1))
    ).astype(int)

    return pd.DataFrame({
        "higher_high": hh.astype(int),
        "higher_low": hl.astype(int),
        "lower_high": lh.astype(int),
        "lower_low": ll.astype(int),
        "bullish_bos": bullish_bos,
        "bearish_bos": bearish_bos,
    }, index=df.index)


# ---------------------------------------------------------------------------
# Multi-timeframe: weekly trend as higher-TF filter
# ---------------------------------------------------------------------------

def weekly_trend(df: pd.DataFrame, sma_period: int = 20) -> pd.Series:
    """
    Weekly trend direction using SMA on weekly closes.
    Returns daily series: 1 = weekly uptrend, -1 = weekly downtrend, 0 = flat.
    Resampled back to daily for alignment.
    """
    weekly = df["close"].resample("W-FRI").last().dropna()
    weekly_sma = weekly.rolling(sma_period).mean()

    trend = pd.Series(0, index=weekly.index)
    trend[weekly > weekly_sma] = 1
    trend[weekly < weekly_sma] = -1

    # Forward-fill to daily
    daily_trend = trend.reindex(df.index, method="ffill")
    return daily_trend.fillna(0).astype(int)


# ---------------------------------------------------------------------------
# Combined signal scorer
# ---------------------------------------------------------------------------

def detect_all_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run all pattern detectors on a single ticker's OHLCV DataFrame.
    Returns a DataFrame with all pattern columns + a composite score.

    Score logic:
      Bullish: +1 per bullish pattern (engulfing, hammer, bullish BoS)
               +1 if weekly uptrend
               -1 per bearish pattern
      Entry signal when score >= 2 (multiple confirmations)
    """
    eng = detect_engulfing(df)
    pin = detect_pin_bar(df)
    doj = detect_doji(df)
    struct = detect_structure(df)
    wt = weekly_trend(df)

    patterns = pd.concat([eng, pin, doj, struct], axis=1)
    patterns["weekly_trend"] = wt

    # Composite bullish score
    patterns["bull_score"] = (
        patterns["bullish_engulfing"]
        + patterns["hammer"]
        + patterns["bullish_bos"]
        + patterns["higher_low"]
        + (patterns["weekly_trend"] == 1).astype(int)
    )

    # Composite bearish score
    patterns["bear_score"] = (
        patterns["bearish_engulfing"]
        + patterns["shooting_star"]
        + patterns["bearish_bos"]
        + patterns["lower_high"]
        + (patterns["weekly_trend"] == -1).astype(int)
    )

    # Net score: positive = bullish, negative = bearish
    patterns["net_score"] = patterns["bull_score"] - patterns["bear_score"]

    # Entry signal: need >= 2 bullish confirmations and no strong bearish
    patterns["bull_signal"] = ((patterns["bull_score"] >= 2) & (patterns["bear_score"] == 0)).astype(int)
    patterns["bear_signal"] = ((patterns["bear_score"] >= 2) & (patterns["bull_score"] == 0)).astype(int)

    return patterns


def scan_universe(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """
    Run pattern detection on all tickers in a data dict.
    Returns {ticker: patterns_df}.
    """
    results = {}
    for ticker, df in data.items():
        if len(df) < 50:
            continue
        results[ticker] = detect_all_patterns(df)
    return results
