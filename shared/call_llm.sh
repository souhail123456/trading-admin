#!/usr/bin/env bash
# Universal LLM caller — replaces call_groq.sh in all bots.
# Uses the API Router with automatic fallback.
#
# Usage: bash call_llm.sh /tmp/prompt.json /tmp/response.json
#
# The prompt.json must have: {"system": "...", "user": "...", "max_tokens": N}
# Or legacy format: {"messages": [...], "max_tokens": N} (auto-converted)
set -euo pipefail

PROMPT_FILE="${1:-/tmp/prompt.json}"
OUTPUT_FILE="${2:-/tmp/response.json}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 - "$PROMPT_FILE" "$OUTPUT_FILE" "$SCRIPT_DIR" << 'PYTHON'
import sys, json, os
sys.path.insert(0, sys.argv[3])
from llm_router import call_llm

prompt_file = sys.argv[1]
output_file = sys.argv[2]

with open(prompt_file) as f:
    data = json.load(f)

# Support legacy format (messages array) or new format (system + user)
if "system" in data and "user" in data:
    system_prompt = data["system"]
    user_prompt = data["user"]
else:
    # Legacy: extract from messages array
    system_prompt = ""
    user_prompt = ""
    for msg in data.get("messages", []):
        if msg["role"] == "system":
            system_prompt = msg["content"]
        elif msg["role"] == "user":
            user_prompt = msg["content"]

max_tokens = data.get("max_tokens", 1500)
temperature = data.get("temperature", 0.3)

response, provider, model = call_llm(system_prompt, user_prompt, max_tokens, temperature)

# Write response in format compatible with existing parsers
output = {
    "choices": [{"message": {"content": response}}],
    "provider": provider,
    "model": model,
}

with open(output_file, "w") as f:
    json.dump(output, f)

print(f"LLM OK — {provider}/{model}")
PYTHON
