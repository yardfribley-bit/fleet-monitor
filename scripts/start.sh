#!/usr/bin/env bash
# fleet-monitor 一键启动：prefect server + 常驻调度 serve
#
# 为什么必须两步：
#   Prefect 3.x 的 `serve` 若连不到真实 server 会退回 ephemeral（内存）模式，
#   而 ephemeral 模式无法调度 —— cron 定时不会真正触发。
#   所以必须先起 `prefect server`（提供 API + 调度器），再起 `serve`（runner）。
#
# 用法：
#   ./scripts/start.sh            # 前台运行（Ctrl+C 停止）
#   ./scripts/start.sh -d         # 后台运行（daemon）
#   ./scripts/start.sh -s         # 额外启动 Streamlit 面板 (:8501)

set -euo pipefail

cd "$(dirname "$0")/.."

VENV=".venv"
PYTHON="$VENV/bin/python"
PREFECT="$VENV/bin/prefect"
STREAMLIT="$VENV/bin/streamlit"

DAEMON=0
WITH_STREAMLIT=0
while getopts "ds" opt; do
  case "$opt" in
    d) DAEMON=1 ;;
    s) WITH_STREAMLIT=1 ;;
    *) echo "用法: $0 [-d] [-s]" >&2; exit 2 ;;
  esac
done

# ---- 0. 环境准备 ----
[ -x "$PYTHON" ] || { echo "错误: 未找到 $PYTHON，请先执行安装步骤"; exit 1; }

# 让本地回环连接（Prefect 连 127.0.0.1）不走系统 SOCKS/HTTP 代理
# macOS 的 proxy_bypass 默认只放行 localhost，不放行 127.0.0.1，会导致
# websockets 报 "SOCKS proxy requires python-socks"。
export NO_PROXY="127.0.0.1,localhost,::1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

export PREFECT_HOME="${PREFECT_HOME:-$PWD/.prefect}"
export PYTHONPATH="src:${PYTHONPATH:-}"

# ---- 1. 启动 prefect server（若未运行）----
SERVER_PORT="${PREFECT_SERVER_PORT:-4200}"
if curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:${SERVER_PORT}/api/health" 2>/dev/null; then
  echo "[1/3] prefect server 已在运行 (http://127.0.0.1:${SERVER_PORT})"
else
  echo "[1/3] 启动 prefect server ..."
  "$PREFECT" server start --port "$SERVER_PORT" > "$PWD/.prefect-server.log" 2>&1 &
  SERVER_PID=$!
  echo "      server PID: $SERVER_PID"
  for i in $(seq 1 30); do
    curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:${SERVER_PORT}/api/health" 2>/dev/null && break
    sleep 2
  done
  curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:${SERVER_PORT}/api/health" 2>/dev/null \
    || { echo "错误: prefect server 启动失败，见 .prefect-server.log"; exit 1; }
  echo "      prefect server 就绪"
fi

export PREFECT_API_URL="http://127.0.0.1:${SERVER_PORT}/api"

# ---- 2. 启动 Streamlit 面板（可选）----
if [ "$WITH_STREAMLIT" -eq 1 ]; then
  [ -x "$STREAMLIT" ] && {
    echo "[2/3] 启动 Streamlit 面板 (http://127.0.0.1:8501) ..."
    "$STREAMLIT" run dashboard/app.py --server.port 8501 \
      --server.headless true > "$PWD/.streamlit.log" 2>&1 &
    echo "      streamlit PID: $!"
  } || echo "[2/3] 未安装 streamlit，跳过面板"
else
  echo "[2/3] 跳过 Streamlit 面板（加 -s 启用）"
fi

# ---- 3. 启动 serve（runner）----
echo "[3/3] 启动常驻调度 serve（cron 见 config/servers.yaml）..."
if [ "$DAEMON" -eq 1 ]; then
  nohup "$PYTHON" -m fleet_monitor.cli serve -n fleet-health-30min \
    > "$PWD/.serve.log" 2>&1 &
  echo "      serve PID: $!（日志 .serve.log）"
  echo "      停止: pkill -f 'fleet_monitor.cli serve'"
else
  exec "$PYTHON" -m fleet_monitor.cli serve -n fleet-health-30min
fi
