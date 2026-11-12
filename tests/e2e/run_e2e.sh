#!/usr/bin/env bash
# 全量场景测试的一键入口。
#
#   tests/e2e/run_e2e.sh up      # 起测试栈（独立端口 18421 + 独立数据卷）
#   tests/e2e/run_e2e.sh run     # 起栈 + 等健康 + 跑全部场景
#   tests/e2e/run_e2e.sh test    # 只跑测试（栈已在跑）
#   tests/e2e/run_e2e.sh logs    # 看 hub 日志
#   tests/e2e/run_e2e.sh down    # 拆栈并删除测试数据卷
#
# PYTHON 可指定带 pytest 的解释器（默认 python3）。
set -euo pipefail

cd "$(dirname "$0")/../.."                      # → agent-hub/
COMPOSE=(docker compose -p agenthub-e2e --env-file tests/e2e/.env.e2e
         -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml)
PY="${PYTHON:-python3}"
URL="http://127.0.0.1:18421"

wait_health() {
  for _ in $(seq 1 60); do
    if curl -fsS "$URL/health" >/dev/null 2>&1; then echo "hub 就绪：$URL"; return 0; fi
    sleep 1
  done
  echo "hub 启动超时，日志：" >&2
  "${COMPOSE[@]}" logs --tail=60 hub >&2
  return 1
}

case "${1:-run}" in
  up)   "${COMPOSE[@]}" up -d --build && wait_health ;;
  run)  "${COMPOSE[@]}" up -d --build && wait_health
        AGENT_HUB_E2E_URL="$URL" "$PY" -m pytest tests/e2e -p no:cacheprovider \
          --basetemp=/tmp/ahtest-e2e "${@:2}" ;;
  test) AGENT_HUB_E2E_URL="$URL" "$PY" -m pytest tests/e2e -p no:cacheprovider \
          --basetemp=/tmp/ahtest-e2e "${@:2}" ;;
  logs) "${COMPOSE[@]}" logs --tail=200 hub ;;
  down) "${COMPOSE[@]}" down -v --remove-orphans ;;
  *)    echo "用法：$0 {up|run|test|logs|down}" >&2; exit 2 ;;
esac
