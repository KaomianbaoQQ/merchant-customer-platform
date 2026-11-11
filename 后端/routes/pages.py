"""页面与图片（前端静态资源）。

三个前端互相独立、同源部署，统一放在「前端/」目录，由本模块提供页面：
    /          顾客商城（客户前端）
    /merchant  商家后台（商户界面）
    /platform  平台管理（平台界面）
    /entry     统一入口导航页（三个前端的入口）
"""
import os

from flask import Blueprint, send_from_directory

from 公共 import store

# 后端/routes/pages.py → 上溯三层到项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(BASE_DIR, "前端")
FRONTEND_FILE = "客户前端.html"
MERCHANT_FILE = "商户界面.html"
PLATFORM_FILE = "平台界面.html"
ENTRY_FILE = "入口.html"

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, FRONTEND_FILE)


@bp.get("/entry")
def entry():
    """统一入口导航页：顾客商城 / 商家后台 / 平台管理。"""
    return send_from_directory(FRONTEND_DIR, ENTRY_FILE)


@bp.get("/merchant")
def merchant():
    """商户管理界面（与客户前端同源，便于调试与联动）。"""
    return send_from_directory(FRONTEND_DIR, MERCHANT_FILE)


@bp.get("/platform")
def platform():
    """平台管理界面（管理员：仲裁 / 商家管控 / 商品与评价 / 对话）。"""
    return send_from_directory(FRONTEND_DIR, PLATFORM_FILE)


@bp.get("/images/<path:filename>")
def image(filename):
    """优先返回商家后台上传的图片，找不到再回退到历史图片目录。"""
    path = store.resolve_image(filename)
    if path:
        return send_from_directory(os.path.dirname(path), os.path.basename(path))
    return ("", 404)


@bp.route("/api/<path:_any>", methods=["OPTIONS"])
def _preflight(_any):
    return ("", 204)