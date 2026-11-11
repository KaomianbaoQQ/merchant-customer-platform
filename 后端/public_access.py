"""公网访问限制：外网只能打开「顾客商城」，商户后台 / 平台管理仅限本机。

背景：`工具/公网访问.command` 用 Cloudflare 快速隧道，只把本机 8000 端口发布到公网，
Dify（80）、MySQL（3306）等本地服务不对外暴露。

隧道请求由本机 cloudflared 转发进来，Flask 看到的 remote_addr 一律是 127.0.0.1，
所以这里同时看代理头（Cf-Connecting-Ip / X-Forwarded-For）来判断请求是否来自公网。
"""
from flask import request

LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}

# 隧道 / 代理注入的头：出现即说明请求来自公网（本机浏览器不会带这些头）
PROXY_HEADERS = ("Cf-Connecting-Ip", "X-Real-Ip", "X-Forwarded-For", "Cf-Ray")

# 公网允许访问的路径：顾客商城页面 + 商品图片 + 顾客侧接口
PUBLIC_PATHS = (
    "/",
    "/images",
    "/api/health",
    "/api/login",
    "/api/logout",
    "/api/register",
    "/api/change-password",
    "/api/reset-password",
    "/api/forgot-account",
    "/api/bind-email",
    "/api/email",
    "/api/cart",
    "/api/products",
    "/api/behavior",
    "/api/regions",
    "/api/orders",
    "/api/addresses",
    "/api/returns",
    "/api/chat",
    "/api/consult",
    "/api/after-sales",
    "/api/logistics",
)

# 公网禁止访问的路径：商户后台、平台管理、统一入口导航
PRIVATE_PATHS = ("/merchant", "/platform", "/entry", "/api/merchant", "/api/platform")


def client_ip():
    """真实来访 IP：优先代理头，其次 remote_addr。"""
    for header in ("Cf-Connecting-Ip", "X-Real-Ip"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def is_external():
    """请求是否来自本机以外（公网隧道 / 局域网其它设备）。"""
    if any(request.headers.get(h) for h in PROXY_HEADERS):
        return True
    return client_ip() not in LOOPBACK


def allow_public(path):
    """公网请求只放行顾客商城的页面与接口。"""
    if any(path.startswith(p) for p in PRIVATE_PATHS):
        return False
    if path == "/":
        return True
    return any(path.startswith(p) for p in PUBLIC_PATHS)


def guard():
    """before_request 钩子：拦住外部对商户 / 平台相关地址的访问。"""
    if request.method == "OPTIONS" or not is_external():
        return None
    if allow_public(request.path):
        return None
    if request.path.startswith("/api/"):
        return {"ok": False, "error": "该接口仅限本机访问"}, 403
    return (
        "<meta charset='utf-8'><h3>商户后台 / 平台管理仅限本机访问</h3>"
        "<p>公网只开放顾客商城，请访问 <a href='/'>商城首页</a>。</p>",
        403,
        {"Content-Type": "text/html; charset=utf-8"},
    )