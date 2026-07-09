"""
API Health Monitor
------------------
Checks the health/status of all external APIs across the 3 bots.
Reports which services are up, degraded, or down.

Usage:
    python3 -m pipeline.agents.api_health
    python3 -m pipeline.agents.api_health --json
"""

import argparse
import json
import os
import time
from datetime import datetime

import requests


# ---------------------------------------------------------------------------
# API definitions
# ---------------------------------------------------------------------------

APIS = [
    # Trading Admin
    {
        "name": "GitHub PAT",
        "bot": "Trading Admin",
        "env_key": "GH_TOKEN",
        "check": "github",
        "critical": True,
        "fallback": "Create new fine-grained PAT at github.com/settings/personal-access-tokens",
    },
    {
        "name": "Telegram Bot",
        "bot": "Trading Admin",
        "env_key": "TELEGRAM_BOT_TOKEN",
        "check": "telegram",
        "critical": True,
        "fallback": "Create new bot via @BotFather on Telegram",
    },
    # FX Bot
    {
        "name": "Capital.com",
        "bot": "FX Bot",
        "env_key": "CAPITAL_API_KEY",
        "check": "capital",
        "critical": True,
        "fallback": "Switch to OANDA: set OANDA_API_KEY + OANDA_ACCOUNT_ID. Or cTrader: set CTRADER_* vars",
    },
    {
        "name": "yfinance (market data)",
        "bot": "FX Bot",
        "env_key": None,
        "check": "yfinance",
        "critical": False,
        "fallback": "No API key needed. If Yahoo Finance is down, data fetch will fail silently",
    },
    # Stock Bot
    {
        "name": "Alpaca",
        "bot": "Stock Bot",
        "env_key": "ALPACA_API_KEY",
        "check": "alpaca",
        "critical": True,
        "fallback": "Regenerate keys at app.alpaca.markets/brokerage/dashboard/overview",
    },
    {
        "name": "Anthropic (Claude)",
        "bot": "Stock Bot",
        "env_key": "ANTHROPIC_API_KEY",
        "check": "anthropic",
        "critical": True,
        "fallback": "Regenerate at console.anthropic.com. Stock bot agent needs this to reason",
    },
    # Polymarket Bot
    {
        "name": "Groq (Llama 3.3 70B)",
        "bot": "Polymarket Bot",
        "env_key": "GROQ_API_KEY",
        "check": "groq",
        "critical": True,
        "fallback": "Switch to OpenAI or Anthropic. Update bot.py OPENAI client config",
    },
    {
        "name": "Polymarket Gamma API",
        "bot": "Polymarket Bot",
        "env_key": None,
        "check": "polymarket",
        "critical": False,
        "fallback": "Public API, no key needed. If down, bot skips cycle",
    },
]


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _check_github(timeout: int = 10) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("BOT_GITHUB_TOKEN")
    if not token:
        return {"status": "no_key", "message": "GH_TOKEN not set"}
    try:
        t0 = time.time()
        r = requests.get("https://api.github.com/rate_limit",
                          headers={"Authorization": f"token {token}"}, timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.status_code == 200:
            data = r.json()
            remaining = data["rate"]["remaining"]
            limit = data["rate"]["limit"]
            return {"status": "ok", "latency_ms": latency,
                    "message": f"Rate limit: {remaining}/{limit} remaining"}
        elif r.status_code == 401:
            return {"status": "error", "message": "Token expired or revoked"}
        else:
            return {"status": "error", "message": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_telegram(timeout: int = 10) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"status": "no_key", "message": "TELEGRAM_BOT_TOKEN not set"}
    try:
        t0 = time.time()
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            bot = r.json().get("result", {})
            return {"status": "ok", "latency_ms": latency,
                    "message": f"Bot: @{bot.get('username', '?')}"}
        else:
            return {"status": "error", "message": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_capital(timeout: int = 15) -> dict:
    api_key = os.environ.get("CAPITAL_API_KEY")
    email = os.environ.get("CAPITAL_EMAIL")
    password = os.environ.get("CAPITAL_PASSWORD")
    if not api_key or not email:
        return {"status": "no_key", "message": "CAPITAL_API_KEY or CAPITAL_EMAIL not set"}
    try:
        t0 = time.time()
        r = requests.post("https://demo-api-capital.backend-capital.com/api/v1/session",
                          headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
                          json={"identifier": email, "password": password or ""},
                          timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            return {"status": "ok", "latency_ms": latency, "message": "Session created"}
        elif r.status_code == 401:
            return {"status": "error", "message": "Invalid credentials"}
        else:
            return {"status": "error", "message": f"HTTP {r.status_code}: {r.text[:100]}"}
    except requests.exceptions.Timeout:
        return {"status": "degraded", "message": "Timeout (Capital.com demo API is slow)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_yfinance(timeout: int = 10) -> dict:
    try:
        t0 = time.time()
        import yfinance as yf
        data = yf.download("EURUSD=X", period="1d", progress=False)
        latency = round((time.time() - t0) * 1000)
        if not data.empty:
            return {"status": "ok", "latency_ms": latency, "message": "Market data available"}
        else:
            return {"status": "degraded", "message": "Empty response"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_alpaca(timeout: int = 10) -> dict:
    key = os.environ.get("ALPACA_API_KEY")
    if not key:
        return {"status": "no_key", "message": "Alpaca broker DOWN: ALPACA_API_KEY not set (lives in stock bot repo)"}
    try:
        t0 = time.time()
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        r = requests.get("https://paper-api.alpaca.markets/v2/account",
                          headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                          timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            acct = r.json()
            return {"status": "ok", "latency_ms": latency,
                    "message": f"Balance: ${float(acct.get('equity', 0)):,.2f}"}
        elif r.status_code == 401:
            return {"status": "error", "message": "Alpaca broker DOWN: invalid/expired API key (HTTP 401)"}
        elif r.status_code == 403:
            return {"status": "error", "message": "Alpaca broker DOWN: forbidden — check API key permissions (HTTP 403)"}
        else:
            return {"status": "error", "message": f"Alpaca broker DOWN: HTTP {r.status_code}"}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "Alpaca broker DOWN: connection timed out"}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "Alpaca broker DOWN: connection refused — API may be offline"}
    except Exception as e:
        return {"status": "error", "message": f"Alpaca broker DOWN: {e}"}


def _check_anthropic(timeout: int = 10) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"status": "no_key", "message": "ANTHROPIC_API_KEY not set (lives in stock bot repo)"}
    try:
        t0 = time.time()
        r = requests.get("https://api.anthropic.com/v1/models",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                          timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            return {"status": "ok", "latency_ms": latency, "message": "API accessible"}
        elif r.status_code == 401:
            return {"status": "error", "message": "Invalid API key"}
        else:
            return {"status": "ok", "latency_ms": latency, "message": f"Reachable (HTTP {r.status_code})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_groq(timeout: int = 10) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return {"status": "no_key", "message": "GROQ_API_KEY not set (lives in polymarket bot repo)"}
    try:
        t0 = time.time()
        r = requests.get("https://api.groq.com/openai/v1/models",
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            return {"status": "ok", "latency_ms": latency, "message": "API accessible"}
        elif r.status_code == 401:
            return {"status": "error", "message": "Invalid API key"}
        else:
            return {"status": "ok", "latency_ms": latency, "message": f"Reachable (HTTP {r.status_code})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_polymarket(timeout: int = 10) -> dict:
    try:
        t0 = time.time()
        r = requests.get("https://gamma-api.polymarket.com/markets?limit=1", timeout=timeout)
        latency = round((time.time() - t0) * 1000)
        if r.ok:
            return {"status": "ok", "latency_ms": latency, "message": "Markets API accessible"}
        else:
            return {"status": "error", "message": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


CHECKERS = {
    "github": _check_github,
    "telegram": _check_telegram,
    "capital": _check_capital,
    "yfinance": _check_yfinance,
    "alpaca": _check_alpaca,
    "anthropic": _check_anthropic,
    "groq": _check_groq,
    "polymarket": _check_polymarket,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def check_all() -> list[dict]:
    """Run all health checks and return results."""
    results = []
    for api in APIS:
        checker = CHECKERS.get(api["check"])
        if checker:
            result = checker()
        else:
            result = {"status": "unknown", "message": "No checker defined"}

        results.append({
            **api,
            **result,
            "checked_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return results


FALLBACK_MAP = {
    "groq": {"llm_fallback": "gemini"},
    "capital": {"fx_broker_status": "down", "fx_action": "skip_new_entries"},
    "alpaca": {"stock_broker_status": "down", "stock_action": "skip_all"},
    "telegram": {"telegram_status": "down"},
    "github": {"github_status": "down"},
    "yfinance": {"market_data_status": "degraded"},
    "polymarket": {"polymarket_api_status": "down"},
    # anthropic: not currently used by any bot — skip
}

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "global_state.json")


def auto_fallback(results: list[dict]) -> dict:
    """Build fallback flags for failing APIs and merge into shared/global_state.json."""
    fallbacks: dict = {}
    for r in results:
        check_key = r.get("check", "")
        if r["status"] in ("error", "no_key", "degraded") and check_key in FALLBACK_MAP:
            fallbacks.update(FALLBACK_MAP[check_key])

    service_status = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        **fallbacks,
    }

    # Merge into existing global_state.json
    state_path = os.path.normpath(STATE_PATH)
    state: dict = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}

    state["service_status"] = service_status

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    return service_status


def print_report(results: list[dict]):
    """Print human-readable health report."""
    print(f"\n{'='*70}")
    print(f"API HEALTH CHECK — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    by_bot = {}
    for r in results:
        by_bot.setdefault(r["bot"], []).append(r)

    icons = {"ok": "+", "degraded": "~", "error": "X", "no_key": "-", "unknown": "?"}
    total_ok = sum(1 for r in results if r["status"] == "ok")
    total_err = sum(1 for r in results if r["status"] == "error")

    for bot, apis in by_bot.items():
        print(f"\n  {bot}:")
        for a in apis:
            icon = icons.get(a["status"], "?")
            latency = f" ({a['latency_ms']}ms)" if "latency_ms" in a else ""
            crit = " [CRITICAL]" if a.get("critical") and a["status"] == "error" else ""
            print(f"    [{icon}] {a['name']}: {a['message']}{latency}{crit}")
            if a["status"] in ("error", "no_key") and a.get("fallback"):
                print(f"        Fallback: {a['fallback']}")

    print(f"\n  Summary: {total_ok}/{len(results)} OK, {total_err} errors")


def main():
    parser = argparse.ArgumentParser(description="API Health Monitor")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--fallback", action="store_true",
                        help="Write fallback flags to shared/global_state.json for failing APIs")
    args = parser.parse_args()

    results = check_all()

    if args.fallback:
        fb = auto_fallback(results)
        import sys
        print(f"Fallback flags written: {json.dumps(fb)}", file=sys.stderr)

    if args.json:
        # Clean non-serializable fields
        clean = []
        for r in results:
            clean.append({k: v for k, v in r.items() if k != "check"})
        print(json.dumps(clean, indent=2))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
