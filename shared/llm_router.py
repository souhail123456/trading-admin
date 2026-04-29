#!/usr/bin/env python3
"""
LLM API Router — Single entry point for all bot LLM calls.
Auto-fallback chain: Groq → Gemini → Cerebras
Tracks usage in shared/api_usage.json

Usage:
    from shared.llm_router import call_llm
    response = call_llm(system_prompt, user_prompt, max_tokens=1500)

Environment variables (set whichever you have):
    GROQ_API_KEY
    GEMINI_API_KEY
    CEREBRAS_API_KEY
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_FILE = Path(__file__).parent / "api_usage.json"

# --- Provider configs ---

PROVIDERS = [
    {
        "name": "groq",
        "env_key": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "fallback_model": "llama-3.1-8b-instant",
        "format": "openai",
    },
    {
        "name": "gemini",
        "env_key": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        "model": "gemini-2.0-flash",
        "format": "gemini",
    },
    {
        "name": "cerebras",
        "env_key": "CEREBRAS_API_KEY",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "model": "llama-3.3-70b",
        "format": "openai",
    },
]


def _log_usage(provider, model, tokens_in, tokens_out, success, error=None):
    """Append usage record to api_usage.json."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "success": success,
        "error": error,
    }

    usage = []
    if USAGE_FILE.exists():
        try:
            usage = json.loads(USAGE_FILE.read_text())
        except:
            usage = []

    usage.append(record)

    # Keep last 500 records only
    if len(usage) > 500:
        usage = usage[-500:]

    USAGE_FILE.write_text(json.dumps(usage, indent=2))


def _call_openai_format(url, api_key, model, system_prompt, user_prompt, max_tokens, temperature):
    """Call OpenAI-compatible API (Groq, Cerebras)."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _call_gemini_format(url, api_key, model, system_prompt, user_prompt, max_tokens, temperature):
    """Call Google Gemini API."""
    full_url = url.format(model=model, api_key=api_key)
    payload = json.dumps({
        "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }).encode()

    req = urllib.request.Request(full_url, data=payload, headers={
        "Content-Type": "application/json",
    })

    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    content = data["candidates"][0]["content"]["parts"][0]["text"]
    # Gemini doesn't always return token counts in same format
    usage_meta = data.get("usageMetadata", {})
    return content, usage_meta.get("promptTokenCount", 0), usage_meta.get("candidatesTokenCount", 0)


def call_llm(system_prompt, user_prompt, max_tokens=1500, temperature=0.3):
    """
    Call LLM with automatic fallback chain.
    Returns: (response_text, provider_used, model_used)
    Raises: RuntimeError if ALL providers fail.
    """
    errors = []

    for provider in PROVIDERS:
        api_key = os.environ.get(provider["env_key"])
        if not api_key:
            continue

        models_to_try = [provider["model"]]
        if "fallback_model" in provider:
            models_to_try.append(provider["fallback_model"])

        for model in models_to_try:
            try:
                if provider["format"] == "openai":
                    content, tokens_in, tokens_out = _call_openai_format(
                        provider["url"], api_key, model,
                        system_prompt, user_prompt, max_tokens, temperature
                    )
                elif provider["format"] == "gemini":
                    content, tokens_in, tokens_out = _call_gemini_format(
                        provider["url"], api_key, model,
                        system_prompt, user_prompt, max_tokens, temperature
                    )
                else:
                    continue

                _log_usage(provider["name"], model, tokens_in, tokens_out, True)
                return content, provider["name"], model

            except Exception as e:
                error_msg = str(e)[:200]
                errors.append(f"{provider['name']}/{model}: {error_msg}")
                _log_usage(provider["name"], model, 0, 0, False, error_msg)

                # If rate limited on primary, try fallback model
                if "rate_limit" in error_msg.lower() or "429" in error_msg:
                    continue
                # If other error, skip to next provider
                else:
                    break

    raise RuntimeError(f"All LLM providers failed:\n" + "\n".join(errors))


def get_usage_summary():
    """Get API usage summary for monitoring."""
    if not USAGE_FILE.exists():
        return {"total_calls": 0, "providers": {}}

    usage = json.loads(USAGE_FILE.read_text())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    summary = {"total_calls": len(usage), "today": {}, "failures_today": 0}
    for record in usage:
        if record["timestamp"].startswith(today):
            provider = record["provider"]
            if provider not in summary["today"]:
                summary["today"][provider] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "failures": 0}
            summary["today"][provider]["calls"] += 1
            summary["today"][provider]["tokens_in"] += record.get("tokens_in", 0)
            summary["today"][provider]["tokens_out"] += record.get("tokens_out", 0)
            if not record["success"]:
                summary["today"][provider]["failures"] += 1
                summary["failures_today"] += 1

    return summary


if __name__ == "__main__":
    # Quick test
    print("Testing LLM Router...")
    try:
        resp, provider, model = call_llm(
            "You are a helpful assistant.",
            "Say 'router works' and nothing else.",
            max_tokens=20
        )
        print(f"OK — {provider}/{model}: {resp}")
    except RuntimeError as e:
        print(f"FAILED: {e}")

    print("\nUsage summary:")
    print(json.dumps(get_usage_summary(), indent=2))
