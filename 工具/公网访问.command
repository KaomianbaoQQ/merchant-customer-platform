#!/bin/bash
# 测试商城公网访问：把本机 8000 端口（顾客商城）通过 Cloudflare 快速隧道发布到公网
#
#   - 只发布 8000 端口，Dify（80）、MySQL（3306）等本地服务不对外暴露
#   - 外网只能打开顾客商城；商户后台 / 平台管理 / 商户接口仍只限本机
#   - 后端没在跑时会顺手拉起后端（关掉本窗口会一并停掉）
#   - 每次运行都会生成一个全新的随机地址，关掉窗口重新运行即换地址
cd "$(dirname "$0")/.." || exit 1

PORT="${1:-8000}"
BACKEND="http://127.0.0.1:${PORT}"

PID=""
BACKEND_PID=""
cleanup() {
  [ -n "${PID}" ] && kill ${PID} 2>/dev/null
  [ -n "${BACKEND_PID}" ] && kill ${BACKEND_PID} 2>/dev/null
}
trap cleanup EXIT INT TERM

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ 未找到 cloudflared，请先执行： brew install cloudflared"
  read -r -p "按回车键关闭…" _
  exit 1
fi

# 后端没在跑：顺手启动（日志见临时文件，失败时会打印出来）
if ! curl -s -m 2 "${BACKEND}/api/health" >/dev/null 2>&1; then
  if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  else
    echo "❌ 未找到 python3，请先安装 Python 3"
    read -r -p "按回车键关闭…" _
    exit 1
  fi
  BACKEND_LOG="$(mktemp -t testmall_backend)"
  echo "后端未运行，正在启动后端（端口 ${PORT}）…"
  "${PY}" -u -c "from 后端.app import run_server; run_server(port=${PORT})" >"${BACKEND_LOG}" 2>&1 &
  BACKEND_PID=$!
  for _ in $(seq 1 30); do
    curl -s -m 2 "${BACKEND}/api/health" >/dev/null 2>&1 && break
    sleep 1
  done
  if ! curl -s -m 2 "${BACKEND}/api/health" >/dev/null 2>&1; then
    echo "❌ 后端启动失败，日志如下："
    tail -n 15 "${BACKEND_LOG}"
    read -r -p "按回车键关闭…" _
    exit 1
  fi
  echo "✅ 后端已就绪（本窗口关闭时会一并停掉）"
fi

echo "正在开启 Cloudflare 隧道（只发布 ${PORT} 端口）…"
LOG="$(mktemp -t cloudflared)"
# 固定用 HTTP/2（443 端口）而不是默认 QUIC（7844 端口）：
# 部分校园网 / 公司网 / 运营商只放行 443，QUIC 会反复 "no recent network activity" 掉线，
# 表现为隧道建好后访问报 Cloudflare 1033 / 522。HTTP/2 走 443，稳定性明显更好。
PROTOCOL="${KB_TUNNEL_PROTOCOL:-http2}"
cloudflared tunnel --no-autoupdate --protocol "${PROTOCOL}" --url "${BACKEND}" >"${LOG}" 2>&1 &
PID=$!

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "${LOG}" | head -n 1)"
  [ -n "${URL}" ] && break
  sleep 1
done

if [ -z "${URL}" ]; then
  echo "❌ 隧道启动失败，日志如下："
  tail -n 20 "${LOG}"
  read -r -p "按回车键关闭…" _
  exit 1
fi

echo ""
echo "======================================================"
echo " 公网地址（发给别人即可打开顾客商城）："
echo "   ${URL}/"
echo "======================================================"
echo ""
echo " 商户后台 / 平台管理：仅本机访问  http://127.0.0.1:${PORT}/merchant  ｜  /platform"
echo " Dify、MySQL 仍只在本机运行，未对外暴露。"
echo " 每次运行都会换一个新地址，关掉本窗口即断开公网访问。"
echo ""
wait ${PID}