"""
Regime Detection Agent
----------------------
Classifies current market regime to guide strategy activation/pausing.

Regimes:
  TRENDING   — ADX > 25, clear directional move → trend strategies active
  RANGING    — ADX < 20, VIX < 20 → mean-reversion strategies (if any)
  VOLATILE   — VIX > 25, ADX any → reduce position sizes, widen stops
  CRISIS     — VIX > 35, sharp drawdowns → risk off, pause all

Inputs: VIX (^VIX), ADX of major pairs, market breadth
Output: regime classification + recommended actions per strategy

Usage:
    python3 -m pipeline.agents.regime_detector
"""

import argparse
import json
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from pipeline.db import init_db, log_agent_action, KILLED_STRATEGIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Regime thresholds
VIX_CRISIS = 35
VIX_VOLATILE = 25
VIX_CALM = 20
ADX_TRENDING = 25
ADX_RANGING = 20


def compute_adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Compute ADX (Average Directional Index) from OHLC data."""
    if len(df) < period * 2:
        return None

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values

    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))

    plus_dm = np.where((high[1:] - high[:-1]) > (low[:-1] - low[1:]),
                       np.maximum(high[1:] - high[:-1], 0), 0)
    minus_dm = np.where((low[:-1] - low[1:]) > (high[1:] - high[:-1]),
                        np.maximum(low[:-1] - low[1:], 0), 0)

    # Smoothed averages
    atr = pd.Series(tr).ewm(span=period, adjust=False).mean().values
    plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean().values / np.where(atr > 0, atr, 1)
    minus_di = 100 * pd.Series(minus_dm).ewm(span=period, adjust=False).mean().values / np.where(atr > 0, atr, 1)

    dx = 100 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) > 0, (plus_di + minus_di), 1)
    adx = pd.Series(dx).ewm(span=period, adjust=False).mean().values

    return float(adx[-1]) if len(adx) > 0 else None


def get_vix() -> float | None:
    """Fetch current VIX level."""
    try:
        vix = yf.download("^VIX", period="5d", progress=False)
        if not vix.empty:
            val = vix["Close"].iloc[-1]
            return float(val.iloc[0]) if hasattr(val, 'iloc') else float(val)
    except Exception as e:
        log.warning(f"Failed to fetch VIX: {e}")
    return None


def _flatten_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance MultiIndex columns (introduced in yfinance >=0.2.x)."""
    if isinstance(df.columns, pd.MultiIndex):
        # e.g. ('Close', 'EURUSD=X') → 'Close'
        df = df.copy()
        df.columns = [col[0] for col in df.columns]
    return df


def get_fx_adx() -> dict[str, float]:
    """Compute ADX for major FX pairs.

    Primary source: yfinance (EURUSD=X format).
    Fallback: Capital.com broker candles if yfinance returns empty or fails.
    """
    pairs = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X"}
    results = {}

    for name, ticker in pairs.items():
        try:
            df = yf.download(ticker, period="60d", progress=False, auto_adjust=True)
            df = _flatten_yf_df(df)
            if not df.empty and "High" in df.columns and len(df) >= 30:
                adx = compute_adx(df)
                if adx is not None:
                    results[name] = round(adx, 1)
                    log.info(f"ADX {name} (yfinance): {adx:.1f}")
                    continue
        except Exception as e:
            log.warning(f"yfinance failed for {name}: {e}")

        # Fallback: Capital.com candles
        try:
            from pipeline.agents.broker_capital import CapitalBroker
            broker = CapitalBroker()
            candles = broker.get_candles(name, granularity="DAY", count=60)
            broker.disconnect()
            if candles and len(candles) >= 30:
                df = pd.DataFrame(candles).rename(columns={
                    "open": "Open", "high": "High", "low": "Low",
                    "close": "Close", "volume": "Volume",
                })
                adx = compute_adx(df)
                if adx is not None:
                    results[name] = round(adx, 1)
                    log.info(f"ADX {name} (Capital.com fallback): {adx:.1f}")
        except Exception as e:
            log.warning(f"Capital.com fallback failed for {name}: {e}")

    return results


def _classify_pair_regime(adx: float) -> str:
    """Classify a single pair's regime based on its ADX value."""
    if adx >= ADX_TRENDING:
        return "TRENDING"
    elif adx <= ADX_RANGING:
        return "RANGING"
    else:
        return "NEUTRAL"


def classify_regime(vix: float | None, fx_adx: dict[str, float]) -> dict:
    """Classify market regime based on VIX and per-pair ADX.

    Global regime (CRISIS/VOLATILE) is driven by VIX and overrides everything.
    Per-pair regime is derived independently from each pair's ADX — a pair with
    ADX > 25 is TRENDING regardless of what other pairs are doing, and a pair
    with ADX < 20 is RANGING regardless.
    """
    # Default to 22 (neutral zone between RANGING=20 and TRENDING=25) when no data
    avg_adx = np.mean(list(fx_adx.values())) if fx_adx else 22

    # --- Global regime (VIX-driven crisis/volatility) ---
    if vix is not None and vix >= VIX_CRISIS:
        global_regime = "CRISIS"
        description = f"VIX {vix:.1f} — extreme fear, risk off"
    elif vix is not None and vix >= VIX_VOLATILE:
        global_regime = "VOLATILE"
        description = f"VIX {vix:.1f} — elevated volatility"
    elif avg_adx >= ADX_TRENDING:
        global_regime = "TRENDING"
        description = f"Avg ADX {avg_adx:.1f} — strong directional moves"
    elif avg_adx <= ADX_RANGING:
        global_regime = "RANGING"
        description = f"Avg ADX {avg_adx:.1f} — low directional movement"
    else:
        global_regime = "TRENDING"
        description = f"Avg ADX {avg_adx:.1f} — moderate trend"

    # --- Per-pair regime classification ---
    # Each pair gets its own trend verdict independent of other pairs.
    # Global CRISIS/VOLATILE overrides per-pair to apply risk-off everywhere.
    pair_regimes: dict[str, dict] = {}
    for pair, adx in fx_adx.items():
        if global_regime in ("CRISIS", "VOLATILE"):
            pair_regime = global_regime
            pair_action = "PAUSE" if global_regime == "CRISIS" else "REDUCE"
            pair_reason = f"Global {global_regime} (VIX={vix:.1f}) overrides pair ADX"
        else:
            pair_regime = _classify_pair_regime(adx)
            if pair_regime == "TRENDING":
                pair_action = "ACTIVE"
                pair_reason = f"ADX {adx:.1f} > {ADX_TRENDING} — trending, trend entries OK"
            elif pair_regime == "RANGING":
                pair_action = "REDUCE"
                pair_reason = f"ADX {adx:.1f} < {ADX_RANGING} — ranging, avoid trend entries"
            else:
                pair_action = "ACTIVE"
                pair_reason = f"ADX {adx:.1f} neutral ({ADX_RANGING}-{ADX_TRENDING}) — moderate trend"
        pair_regimes[pair] = {
            "regime": pair_regime,
            "adx": adx,
            "action": pair_action,
            "reason": pair_reason,
        }

    # --- Strategy-level recommendations (global, backward-compatible) ---
    recommendations = {}

    if global_regime == "CRISIS":
        recommendations = {
            100: {"action": "PAUSE", "reason": "Crisis mode — no new trend entries"},
            101: {"action": "PAUSE", "reason": "Crisis mode — no new PA entries"},
        }
    elif global_regime == "VOLATILE":
        recommendations = {
            100: {"action": "REDUCE", "reason": "High vol — halve position sizes, widen stops"},
            101: {"action": "PAUSE", "reason": "High vol — PA patterns unreliable"},
        }
    elif global_regime == "RANGING":
        recommendations = {
            100: {"action": "REDUCE", "reason": "Ranging market — trend signals weaker"},
            101: {"action": "ACTIVE", "reason": "Ranging favors price action reversals"},
        }
    else:  # TRENDING / NEUTRAL
        recommendations = {
            100: {"action": "ACTIVE", "reason": "Trending market — ideal for trend strategy"},
            101: {"action": "ACTIVE", "reason": "Trending with PA confirmation"},
        }

    # Override recommendations for permanently killed strategies
    for sid, kill_reason in KILLED_STRATEGIES.items():
        if sid in recommendations:
            recommendations[sid] = {"action": "KILLED", "reason": kill_reason}

    return {
        "regime": global_regime,
        "description": description,
        "vix": round(vix, 1) if vix else None,
        "avg_adx": round(avg_adx, 1),
        "fx_adx": fx_adx,
        "pair_regimes": pair_regimes,
        "recommendations": recommendations,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def run_regime_detection(db_path: str | None = None) -> dict:
    """Run full regime detection."""
    conn = init_db(db_path)

    print(f"\n{'='*60}")
    print(f"REGIME DETECTOR — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    print("\n  Fetching VIX...")
    vix = get_vix()
    print(f"  VIX: {vix or 'N/A'}")

    print("\n  Computing FX ADX...")
    fx_adx = get_fx_adx()
    for pair, adx in fx_adx.items():
        print(f"    {pair}: ADX {adx}")

    result = classify_regime(vix, fx_adx)

    print(f"\n  REGIME: {result['regime']}")
    print(f"  {result['description']}")

    print(f"\n  PER-PAIR REGIMES:")
    for pair, pr in result.get("pair_regimes", {}).items():
        print(f"    {pair}: {pr['regime']} (ADX={pr['adx']:.1f}) — {pr['action']}")

    print(f"\n  STRATEGY RECOMMENDATIONS:")
    for sid, rec in result["recommendations"].items():
        name = {100: "FX Trend", 101: "FX Price Action"}.get(sid, f"Strategy {sid}")
        print(f"    [{sid}] {name}: {rec['action']} — {rec['reason']}")

    log_agent_action(
        conn, "regime_detector", "regime_classified",
        outputs=result,
    )

    return result


def main():
    parser = argparse.ArgumentParser(description="Regime Detector")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()
    run_regime_detection(db_path=args.db)


if __name__ == "__main__":
    main()
