#!/usr/bin/env bash
# Review Hub 启动脚本：首次自动建 venv 装依赖，之后直接启动
set -euo pipefail
cd "$(dirname "$0")"
# 需要 Python >= 3.10（用了 int|None 语法）
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { echo "需要 Python >= 3.10（brew install python@3.11）"; exit 1; }
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q fastapi 'uvicorn[standard]' httpx pyyaml python-multipart
fi
# 读取 .env（供 codex 桥接配置生成使用）
if [ -f .env ]; then set -a; source .env; set +a; fi
# 生成 hub 专属 codex 配置（Kimi 后端，Responses API；不影响全局 ~/.codex）
if [ -n "${KIMI_API_KEY:-}" ]; then
  mkdir -p data/codex-home
  cat > data/codex-home/config.toml <<TOML
model_provider = "kimi"
model = "${KIMI_MODEL:-kimi-k2.7-code}"

[model_providers.kimi]
name = "Kimi"
base_url = "${KIMI_BASE_URL:-https://api.moonshot.cn/v1}"
env_key = "KIMI_API_KEY"
wire_api = "responses"
TOML
fi
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${HUB_PORT:-7777}"
