#!/bin/bash
set -e
cd "$(dirname "$0")"

# Kill any existing process on port 8000
lsof -t -i:8000 | xargs kill 2>/dev/null || true

# Provider: pass as first arg, default to anthropic
PROVIDER="${1:-anthropic}"
export MODEL_PROVIDER="$PROVIDER"

if [ "$PROVIDER" = "minimax" ]; then
  if [ -z "$MINIMAX_API_KEY" ]; then
    echo "Error: MINIMAX_API_KEY not set"
    exit 1
  fi
else
  if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "Error: ANTHROPIC_API_KEY not set"
    exit 1
  fi
fi

echo "Starting with provider: $PROVIDER"
python3 -m uvicorn agent:app --reload --port 8000
