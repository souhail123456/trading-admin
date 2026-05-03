"""
Trading Admin (FX Bot) — API Health Check
-------------------------------------------
Tests local APIs (Capital.com, Telegram, GitHub) and writes results to
shared/health_status.json.

Uses `requests` (available in trading-admin's requirements).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

SHANGHAI_TZ = timezone(timedelta(hours=8))
OUT_FILE = "shared/health_status.json"


def check_service(name: str, method: str, url: str, **kwargs) -> dict:
    """Make a request and return a health result dict."""
    t0 = time.monotonic()
    try:
        resp = requests.request(method, url, timeout=10, **kwargs)
        latency_ms = round((time.monotonic() - t0) * 1000)
        if resp.ok:
            return {"status": "ok", "latency_ms": latency_ms}
        else:
            return {"status": "error", "latency_ms": latency_ms, "error": f"{resp.status_code} {resp.reason}"}
    except Exception as e:
        latency_ms = round((time.monotonic() - t0) * 1000)
        return {"status": "error", "latency_ms": latency_ms, "error": str(e)[:120]}


def run_checks() -> dict:
    services = {}

    # Capital.com API
    capital_key = os.environ.get("CAPITAL_API_KEY", "")
    capital_email = os.environ.get("CAPITAL_EMAIL", "")
    capital_password = os.environ.get("CAPITAL_PASSWORD", "")
    if capital_key and capital_email and capital_password:
        services["capital_com"] = check_service(
            "capital_com", "POST",
            "https://api-capital.backend-capital.com/api/v1/session",
            headers={
                "X-CAP-API-KEY": capital_key,
                "Content-Type": "application/json",
            },
            json={
                "identifier": capital_email,
                "key": capital_key,
                "password": capital_password,
            }
        )
    else:
        services["capital_com"] = {"status": "error", "latency_ms": 0, "error": "CAPITAL credentials not set"}

    # Telegram
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tg_token:
        services["telegram"] = check_service(
            "telegram", "GET",
            f"https://api.telegram.org/bot{tg_token}/getMe"
        )
    else:
        services["telegram"] = {"status": "error", "latency_ms": 0, "error": "TELEGRAM_BOT_TOKEN not set"}

    # GitHub API — check rate limit
    gh_token = os.environ.get("BOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if gh_token:
        t0 = time.monotonic()
        try:
            resp = requests.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {gh_token}"},
                timeout=10
            )
            latency_ms = round((time.monotonic() - t0) * 1000)
            if resp.ok:
                data = resp.json()
                core = data.get("resources", {}).get("core", {})
                remaining = core.get("remaining", "?")
                limit = core.get("limit", "?")
                services["github_api"] = {
                    "status": "ok",
                    "latency_ms": latency_ms,
                    "remaining": remaining,
                    "limit": limit,
                }
            else:
                services["github_api"] = {
                    "status": "error",
                    "latency_ms": latency_ms,
                    "error": f"{resp.status_code} {resp.reason}",
                }
        except Exception as e:
            latency_ms = round((time.monotonic() - t0) * 1000)
            services["github_api"] = {"status": "error", "latency_ms": latency_ms, "error": str(e)[:120]}
    else:
        services["github_api"] = {"status": "error", "latency_ms": 0, "error": "BOT_GITHUB_TOKEN not set"}

    return services


def main():
    os.makedirs("shared", exist_ok=True)
    print("Running Trading Admin health checks...")

    services = run_checks()
    checked_at = datetime.now(SHANGHAI_TZ).isoformat()

    result = {
        "checked_at": checked_at,
        "repo": "trading-admin",
        "services": services,
    }

    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    ok_count = sum(1 for s in services.values() if s["status"] == "ok")
    total = len(services)
    print(f"Health check complete: {ok_count}/{total} services healthy")
    for name, info in services.items():
        icon = "✓" if info["status"] == "ok" else "✗"
        extra = ""
        if name == "github_api" and info["status"] == "ok":
            extra = f" ({info.get('remaining')}/{info.get('limit')} remaining)"
        elif info["status"] == "error":
            extra = f" — {info.get('error', '')}"
        print(f"  {icon} {name}: {info['latency_ms']}ms{extra}")
    print(f"Results written to {OUT_FILE}")


if __name__ == "__main__":
    main()
