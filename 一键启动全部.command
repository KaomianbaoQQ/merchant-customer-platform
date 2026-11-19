#!/bin/bash
# 测试商城一键启动全部：Dify（Docker）+ MongoDB + 后端 + 三个网页
#
#   - Dify：进入 DIFY_DIR 执行 docker compose up -d（已在跑则秒过，Docker 没开会自动拉起来）
#   - MongoDB：容器 testmall-mongo 没在跑就启动（聊天记录存在这里，NoSQL）
#   - 后端：8000 端口没在跑就启动（本窗口关闭时会一并停掉）
#   - 三个网页：顾客商城 / 商家后台 / 平台管理 各开一个标签页
#   - 公网访问：需要时再双击「工具/公网访问.command」
#
# 可临时换位置/端口： DIFY_DIR=/别的/dify/docker PORT=8001 双击前先 export
cd "$(dirname "$0")" || exit 1

# 本机服务一律不走系统 HTTP 代理：开了 Clash 之类的全局代理时，
# 发往 localhost 的请求会被代理拦下并返回 502，容易误判成「Dify 挂了」。
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"

PORT="${PORT:-8000}"
DIFY_DIR="${DIFY_DIR:-$HOME/dify/docker}"
DIFY_URL="${DIFY_URL:-http://localhost/}"
LOCAL="http://127.0.0.1:${PORT}"

PID=""
cleanup() { [ -n "${PID}" ] && kill ${PID} 2>/dev/null; }
trap cleanup EXIT INT TERM

# ---------- 1. Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未找到 docker，请先安装并启动 Docker Desktop"
  read -r -p "按回车键关闭…" _
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 没在运行，正在启动 Docker Desktop…"
  open -a Docker >/dev/null 2>&1
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi

if ! docker info >/dev/null 2>&1; then
  echo "❌ Docker 仍未就绪，请手动打开 Docker Desktop 后重试"
  read -r -p "按回车键关闭…" _
  exit 1
fi

# ---------- 2. Dify ----------
if [ ! -f "${DIFY_DIR}/docker-compose.yaml" ]; then
  echo "❌ 找不到 Dify 的 compose 文件：${DIFY_DIR}/docker-compose.yaml"
  echo "   如 Dify 装在别处，请先执行： export DIFY_DIR=/你的/dify/docker"
  read -r -p "按回车键关闭…" _
  exit 1
fi

echo "正在启动 Dify（${DIFY_DIR}）…"
if ! (cd "${DIFY_DIR}" && docker compose up -d); then
  echo "❌ Dify 启动失败，请到 ${DIFY_DIR} 手动执行 docker compose up -d 查看原因"
  read -r -p "按回车键关闭…" _
  exit 1
fi

echo -n "等待 Dify 就绪"
DIFY_OK=""
for _ in $(seq 1 90); do
  if [ "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 3 "${DIFY_URL}")" != "000" ]; then
    DIFY_OK="1"
    break
  fi
  echo -n "."
  sleep 2
done
echo ""
if [ -n "${DIFY_OK}" ]; then
  echo "✅ Dify 已就绪：${DIFY_URL}"
else
  echo "⚠️ Dify 还没响应，客服功能可能暂时不可用，可稍后再试。"
fi

# ---------- 3. MongoDB（聊天记录）----------
MONGO_NAME="testmall-mongo"
if docker inspect -f '{{.State.Running}}' "${MONGO_NAME}" 2>/dev/null | grep -q true; then
  echo "✅ MongoDB 已在运行"
else
  echo "正在启动 MongoDB（${MONGO_NAME}）…"
  if docker inspect "${MONGO_NAME}" >/dev/null 2>&1; then
    docker start "${MONGO_NAME}" >/dev/null
  else
    docker run -d --name "${MONGO_NAME}" -p 127.0.0.1:27017:27017 \
      -v testmall_mongo_data:/data/db --restart unless-stopped mongo:7 >/dev/null
  fi
  for _ in $(seq 1 30); do
    docker inspect -f '{{.State.Running}}' "${MONGO_NAME}" 2>/dev/null | grep -q true && break
    sleep 1
  done
fi

if docker inspect -f '{{.State.Running}}' "${MONGO_NAME}" 2>/dev/null | grep -q true; then
  echo "✅ MongoDB 已就绪（聊天记录 / NoSQL）"
else
  echo "⚠️ MongoDB 未就绪，聊天记录写不进去（只告警，不影响下单与客服）。"
fi

# ---------- 4. 后端 ----------
if curl -s --noproxy '*' -m 2 "${LOCAL}/api/health" >/dev/null 2>&1; then
  echo "✅ 后端已在运行（不在本窗口，关掉本窗口不影响它）"
else
  if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  else
    echo "❌ 未找到 python3，请先安装 Python 3"
    read -r -p "按回车键关闭…" _
    exit 1
  fi

  echo "正在启动后端（端口 ${PORT}）…"
  "$PY" -u main.py &
  PID=$!

  BACKEND_OK=""
  for _ in $(seq 1 30); do
    if curl -s --noproxy '*' -m 2 "${LOCAL}/api/health" >/dev/null 2>&1; then
      BACKEND_OK="1"
      break
    fi
    sleep 1
  done
  if [ -z "${BACKEND_OK}" ]; then
    echo "❌ 后端启动超时，请检查数据库（MySQL cs 库）是否可用，或手动执行：${PY} main.py"
    read -r -p "按回车键关闭…" _
    exit 1
  fi
  echo "✅ 后端已就绪"
fi

# ---------- 5. 三个网页 ----------
open "${LOCAL}/" "${LOCAL}/merchant" "${LOCAL}/platform"

echo ""
echo "顾客商城： ${LOCAL}/"
echo "商家后台： ${LOCAL}/merchant"
echo "平台管理： ${LOCAL}/platform   （管理员 admin / 12345）"
echo "Dify：     ${DIFY_URL}"
echo "公网访问： 需要时再双击「工具/公网访问.command」"
echo ""

if [ -n "${PID}" ]; then
  echo "全部已启动，本窗口是后端进程，关闭本窗口即可停止后端（Dify 容器不受影响）。"
  wait ${PID}
else
  echo "全部已启动（后端由别的窗口负责）。"
fi