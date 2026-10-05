#!/usr/bin/env bash
# Compose verify 服务入口：
#   1) Python 单元测试（重点：分支回滚、精确异常边界）
#   2) 构建检查（全部源码字节码编译）
#   3) API/HTTP 冒烟
#      - 设置 SMOKE_URL（如 http://app:8080）时直接对该服务冒烟；
#      - 否则在本地临时启动服务再冒烟。
# 任一步失败即以非零码退出并返回结果码。
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "${SMOKE_URL:-}" ]]; then
  BASE="${SMOKE_URL}"
else
  PORT="${VERIFY_PORT:-8099}"
  BASE="http://127.0.0.1:${PORT}"
fi

echo "==> [1/3] 单元测试"
python3 -m unittest discover -s tests -v

echo "==> [2/3] 构建检查（compileall）"
python3 -m compileall -q app scripts tests

echo "==> [3/3] API/HTTP 冒烟（${BASE}）"
if [[ -z "${SMOKE_URL:-}" ]]; then
  PORT="${PORT}" HOST=127.0.0.1 python3 app/server.py &
  SERVER_PID=$!
  cleanup() { kill "${SERVER_PID}" 2>/dev/null || true; wait "${SERVER_PID}" 2>/dev/null || true; }
  trap cleanup EXIT
fi

for i in $(seq 1 60); do
  if python3 - "$BASE" <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1] + "/healthz", timeout=1)
except Exception:
    raise SystemExit(1)
PY
  then
    echo "    服务已就绪"
    break
  fi
  if [[ "$i" == "60" ]]; then
    echo "    等待服务健康超时" >&2
    exit 1
  fi
  sleep 1
done

python3 scripts/smoke.py "$BASE"

echo "==> verify 全部通过"
