#!/usr/bin/env bash
# Pull the LLM router from trading_admin into any bot's workflow.
# Usage in GitHub Actions:
#   - name: Setup LLM Router
#     run: |
#       curl -sL https://raw.githubusercontent.com/souhail123456/trading_admin/main/shared/llm_router.py -o /tmp/llm_router.py
#       curl -sL https://raw.githubusercontent.com/souhail123456/trading_admin/main/shared/call_llm.sh -o /tmp/call_llm.sh
#       chmod +x /tmp/call_llm.sh
#
# Then use: bash /tmp/call_llm.sh /tmp/prompt.json /tmp/response.json
# Instead of: bash scripts/call_groq.sh /tmp/prompt.json /tmp/response.json

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/souhail123456/trading_admin/main/shared"

curl -sL "$REPO_RAW/llm_router.py" -o /tmp/llm_router.py
curl -sL "$REPO_RAW/call_llm.sh" -o /tmp/call_llm.sh
chmod +x /tmp/call_llm.sh

# Also pull shared context if available
curl -sL "$REPO_RAW/news.json" -o /tmp/shared_news.json 2>/dev/null || echo "{}" > /tmp/shared_news.json
curl -sL "$REPO_RAW/global_state.json" -o /tmp/shared_global_state.json 2>/dev/null || echo "{}" > /tmp/shared_global_state.json

echo "LLM Router + shared context loaded from trading_admin"
