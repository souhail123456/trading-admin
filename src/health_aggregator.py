"""
Trading Admin — Health Aggregator
-----------------------------------
1. Reads local shared/health_status.json (FX Bot / trading-admin checks)
2. Fetches health_status.json from the other 2 repos via GitHub API
3. Builds a unified Telegram health report and sends it

Env vars required:
  BOT_GITHUB_TOKEN   — to read files from other repos
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import requests

SHANGHAI_TZ = timezone(timedelta(hours=8))

POLYMARKET_REPO = os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot")
TRADING_BOT_REPO = os.environ.get("STOCK_REPO", "souhail123456/trading-bot")


# ---------------------------------------------------------------------------
# Fetch remote health status files via GitHub Contents API
# ---------------------------------------------------------------------------

def _fetch_github_json(repo: str, path: str, gh_token: str) -> dict | None:
    """Fetch a JSON file from a GitHub repo via the Contents API."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  GitHub API error fetching {repo}/{path}: {resp.status_code} {resp.reason}")
            return None
        data = resp.json()
        content_b64 = data.get("content", "")
        # GitHub returns base64 with newlines
        decoded = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        print(f"  Error fetching {repo}/{path}: {e}")
        return None


def load_all_statuses(gh_token: str) -> dict[str, dict | None]:
    print("Fetching health statuses...")

    # Local status (FX bot / trading-admin)
    admin_status = None
    try:
        with open("shared/health_status.json") as f:
            admin_status = json.load(f)
        print("  ✓ trading-admin: loaded from shared/health_status.json")
    except Exception as e:
        print(f"  ✗ trading-admin: {e}")

    # Remote: polymarket-bot
    poly_status = _fetch_github_json(
        POLYMARKET_REPO, "logs/health_status.json", gh_token
    )
    if poly_status:
        print(f"  ✓ polymarket-bot: fetched from GitHub")
    else:
        print(f"  ✗ polymarket-bot: not available")

    # Remote: trading-bot (stock)
    stock_status = _fetch_github_json(
        TRADING_BOT_REPO, "memory/health_status.json", gh_token
    )
    if stock_status:
        print(f"  ✓ trading-bot: fetched from GitHub")
    else:
        print(f"  ✗ trading-bot: not available")

    return {
        "polymarket": poly_status,
        "stock": stock_status,
        "admin": admin_status,
    }


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

SERVICE_LABELS = {
    # polymarket-bot
    "polymarket_gamma": "Polymarket Gamma",
    "coingecko": "CoinGecko",
    "fred": "FRED",
    "groq": "Groq",
    "gemini": "Gemini",
    "cerebras": "Cerebras",
    "telegram": "Telegram",
    # trading-bot (stock)
    "alpaca": "Alpaca",
    # trading-admin (fx)
    "capital_com": "Capital.com",
    "github_api": "GitHub API",
}


def _service_line(svc_key: str, info: dict) -> str:
    label = SERVICE_LABELS.get(svc_key, svc_key)
    if info["status"] == "ok":
        latency = info.get("latency_ms", "?")
        extra = ""
        if svc_key == "github_api":
            remaining = info.get("remaining")
            limit = info.get("limit")
            if remaining is not None:
                extra = f" ({remaining}/{limit})"
        return f"✅ {label} — {latency}ms{extra}"
    else:
        err = info.get("error", "unknown error")
        return f"❌ {label} — {err}"


def _section(title: str, status: dict | None) -> tuple[list[str], int, int]:
    """Return (lines, ok_count, total_count)."""
    lines = [f"\n*{title}*"]
    if status is None:
        lines.append("⚠️ Status unavailable")
        return lines, 0, 0

    ok = 0
    total = 0
    for svc_key, info in status.get("services", {}).items():
        lines.append(_service_line(svc_key, info))
        total += 1
        if info["status"] == "ok":
            ok += 1

    checked = status.get("checked_at", "")
    if checked:
        # Show a short timestamp
        try:
            dt = datetime.fromisoformat(checked)
            dt_sh = dt.astimezone(SHANGHAI_TZ)
            lines.append(f"_checked {dt_sh.strftime('%H:%M')} Shanghai_")
        except Exception:
            pass

    return lines, ok, total


def build_telegram_message(statuses: dict) -> str:
    now_sh = datetime.now(SHANGHAI_TZ).strftime("%H:%M")

    lines = [
        "🏥 *System Health Check*",
        f"🕐 {now_sh} Shanghai",
    ]

    total_ok = 0
    total_all = 0

    poly_lines, ok, tot = _section("Polymarket Bot", statuses["polymarket"])
    lines += poly_lines
    total_ok += ok
    total_all += tot

    stock_lines, ok, tot = _section("Stock Bot", statuses["stock"])
    lines += stock_lines
    total_ok += ok
    total_all += tot

    admin_lines, ok, tot = _section("FX Bot", statuses["admin"])
    lines += admin_lines
    total_ok += ok
    total_all += tot

    lines.append("")
    lines.append(f"Overall: {total_ok}/{total_all} services healthy")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Telegram message sent ({resp.status_code})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    gh_token = os.environ.get("BOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not gh_token:
        print("WARNING: BOT_GITHUB_TOKEN not set — cannot fetch remote statuses")

    statuses = load_all_statuses(gh_token)
    message = build_telegram_message(statuses)

    print("\n--- Telegram message preview ---")
    print(message)
    print("--------------------------------\n")

    send_telegram(message)


if __name__ == "__main__":
    main()
