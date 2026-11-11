"""后端公共工具：把前端传来的用户身份与数据库核对。

管理员身份不再由前端传来的 user_id / user_name 决定 —— 那组值前端可随意伪造，
任何人带上 `user_id=admin&user_name=admin` 就能拿到管理员权限。
现在只有 /api/platform/login 用密码换取的管理员令牌（见 公共/admin_auth.py）
才是管理员凭据，从 Authorization: Bearer <token> 或 X-Platform-Token 读取。
"""
from flask import request

from 公共.admin_auth import verify_token
from 售后.user import verify_user


def admin_from_request():
    """从请求头里取管理员令牌并校验。返回管理员 dict 或 None。

    只认请求头（不放 URL 参数，避免令牌出现在日志 / Referer 里）。
    """
    try:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return verify_token(auth[7:])
        return verify_token(request.headers.get("X-Platform-Token"))
    except RuntimeError:
        return None


def resolve_user(raw):
    """把前端传来的 user 再和数据库核对一遍，返回规范化 user 或 None。

    身份识别优先级：
      1) 顾客令牌（Authorization: Bearer <token>，由 /api/login 签发）；
      2) 管理员令牌（由 /api/platform/login 签发）；
      3) 旧式 id + name 回数据库核对（向后兼容老前端，可被伪造，仅本地演示用）。
    """
    if not isinstance(raw, dict):
        raw = {}

    # 1) 顾客令牌：最优先，避免依赖前端可信的 id/name
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        from 公共.user_auth import verify_customer_token
        cu = verify_customer_token(auth[7:])
        if cu is not None:
            return {"id": str(cu["id"]), "name": cu["name"], "role": "user",
                    "is_admin": False, "age": cu.get("age"), "sex": cu.get("sex"),
                    "token": auth[7:]}

    # 2) 管理员令牌
    admin = admin_from_request()
    if admin is not None:
        return {"id": admin["id"], "name": admin["name"], "role": "admin",
                "is_admin": True, "age": None, "sex": None}

    # 3) 旧式 id + name 核对（兼容，弱）
    user_id = str(raw.get("id") or raw.get("user_id") or "").strip()
    user_name = str(raw.get("name") or raw.get("user_name") or "").strip()
    if not user_id or not user_name:
        return None

    ok, info, _ = verify_user(user_id, user_name)
    if not ok:
        return None
    return {"id": str(info["id"]), "name": info["name"], "role": "user",
            "is_admin": False, "age": info.get("age"), "sex": info.get("sex")}