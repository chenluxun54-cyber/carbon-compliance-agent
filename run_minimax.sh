#!/bin/bash
# 启动 carbon_skill MiniMax 后端
set -e

export MODEL_PROVIDER=minimax
# 填你的 MiniMax API key（不要 commit）
export MINIMAX_API_KEY="${MINIMAX_API_KEY:-在这里填key}"
export PATH="$HOME/Library/Python/3.9/bin:$PATH"

cd ~/Desktop/carbon_skill

echo "==> 启动 uvicorn @ 127.0.0.1:8000 (provider=minimax)"
echo "==> 浏览器打开 http://127.0.0.1:8000"
echo "==> Ctrl+C 停止"
echo

exec uvicorn agent:app --host 127.0.0.1 --port 8000 --reload