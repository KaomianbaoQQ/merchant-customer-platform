"""智能分析接口：行为埋点上报 + 平台侧用户画像 / 购买可能性。

    POST /api/behavior                        顾客浏览 / 加购行为上报（埋点）
    GET  /api/platform/analytics/profiles     平台：全部顾客画像
    GET  /api/platform/analytics/profile/<uid> 平台：单个顾客画像
    GET  /api/platform/analytics/purchase     平台：全部商品的购买可能性 + 潜在买家

平台接口沿用平台管理员令牌鉴权（公共/admin_auth.py）。
商户侧的分析接口在 后端/routes/merchant.py（按登录商家隔离）。
"""
from flask import Blueprint, jsonify, request

from 公共 import store
from 公共 import behavior
from 公共.admin_auth import verify_token
from 智能分析 import 用户画像 as profiles_mod
from 智能分析 import 购买预测 as predict_mod

bp = Blueprint("analytics", __name__, url_prefix="/api")

def _admin():
    auth = request.headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else \
        (request.headers.get("X-Platform-Token") or request.args.get("token") or "")
    return verify_token(token)

def _require_admin():
    if not _admin():
        return jsonify({"ok": False, "error": "请先登录平台管理员账号", "code": "NO_AUTH"}), 401
    return None

# 行为埋点（客户前端调用）
@bp.post("/behavior")
def api_behavior():
    """记录顾客行为：event = view（浏览）/ cart（加购）。

    只在能识别出顾客身份时记录（未登录用户没有 user_id，无法归属到画像）。
    """
    data = request.get_json(silent=True) or {}
    user = data.get("user") or {}
    uid = str(user.get("id") or data.get("user_id") or "").strip()
    event = str(data.get("event") or "").strip()
    pid = str(data.get("product_id") or "").strip()
    if not uid or not pid or event not in behavior.EVENTS:
        return jsonify({"ok": False, "error": "缺少 user / product_id 或 event 非法"}), 400

    product = store.get_product(pid, include_offline=True) or {}
    behavior.record(
        uid, event,
        product_id=pid,
        product_name=product.get("name") or "",
        merchant_id=str(product.get("owner_id") or ""),
        user_name=str(user.get("name") or ""),
    )
    return jsonify({"ok": True})

# 平台：用户画像
@bp.get("/platform/analytics/profiles")
def api_platform_profiles():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "profiles": profiles_mod.list_profiles()})

@bp.get("/platform/analytics/profile/<uid>")
def api_platform_profile_detail(uid):
    err = _require_admin()
    if err:
        return err
    profile = profiles_mod.build_profile(uid)
    if not profile:
        return jsonify({"ok": False, "error": "顾客不存在"}), 404
    return jsonify({"ok": True, "profile": profile})

# 平台：商品购买可能性
@bp.get("/platform/analytics/purchase")
def api_platform_purchase():
    err = _require_admin()
    if err:
        return err
    return jsonify({
        "ok": True,
        "products": predict_mod.product_prospects(),
        "top": predict_mod.top_prospects(k=30),
    })