"""全站准入控制：强制登录 / 角色权限 / 次数限制 / 操作留痕。

由 后端/app.py 挂载：
    app.before_request(access_control.before)
    app.after_request(access_control.after)

四条策略集中在本模块，不改动任何业务路由：
  1. 除登录 / 注册 / 验证码 / 健康检查等入口外，所有 /api/* 必须持有效凭证。
     页面与静态资源放行（否则连登录页都打不开）。
  2. 按角色限定接口：平台管理仅管理员、商户后台仅「已审核」商户、
     顾客业务接口仅顾客 / 管理员；任何登录身份都可用的接口单独列出。
  3. 分级次数限制：通用每分钟、客服类每日配额、登录 / 注册按来源 IP 防爆破。
  4. 所有 /api/* 请求（含被拒绝的）写入 cs.audit_log，密码 / 令牌 / 验证码脱敏。

配置在 config.json 的 access 段；access.enabled = false 可一键停用强制策略，
方便出问题时快速回退。数值改动后重启后端生效。
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from flask import g, jsonify, request

from 公共 import app_config
from 公共.accounts import get_merchant_by_token
from 公共.admin_auth import verify_token as verify_admin_token
from 公共.db_config import get_conn
from 公共.user_auth import verify_customer_token
from .public_access import client_ip

# ---------------------------------------------------------------- 配置

_DEFAULTS = {
    "enabled": True,
    "audit_enabled": True,
    "strict_user_identity": True,
    "general_per_minute": 120,
    "chat_daily_quota": 30,
    "login_per_minute": 10,
    "register_per_hour": 5,
    "identity_cache_seconds": 2,
}


def cfg(key):
    default = _DEFAULTS[key]
    val = app_config.get("access." + key, default)
    if isinstance(default, bool):
        if isinstance(val, str):
            return val.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def is_enabled():
    return bool(cfg("enabled"))


def strict_user_identity():
    """是否禁用「只报 id + name 就认作某顾客」的旧式弱身份识别。"""
    return bool(cfg("strict_user_identity"))


# ---------------------------------------------------------------- 放行名单

# 这些接口必须在不登录时可访问：否则无法登录、注册、收验证码、找回密码。
PUBLIC_API = frozenset((
    "/api/health",
    "/api/regions",
    "/api/login", "/api/logout", "/api/register",
    "/api/email/send-code", "/api/email/status",
    "/api/change-password", "/api/reset-password",
    "/api/forgot-account", "/api/bind-email",
    "/api/merchant/login", "/api/merchant/register", "/api/merchant/logout",
    "/api/platform/login", "/api/platform/logout",
))

# 客服类接口：除每分钟通用限制外，再叠加每日配额（每次提问都会消耗模型额度）
CHAT_API = ("/api/consult", "/api/chat", "/api/after-sales")
LOGIN_API = ("/api/login", "/api/merchant/login", "/api/platform/login", "/api/change-password")
REGISTER_API = ("/api/register", "/api/merchant/register", "/api/email/send-code",
                "/api/reset-password", "/api/forgot-account")

# 任意已登录身份都可使用（埋点 / 图片上传 / 退出）
ANY_ROLE_API = ("/api/behavior", "/api/upload", "/api/logout",
                "/api/merchant/logout", "/api/platform/logout")

# 角色规则：按路径前缀匹配，命中即只放行指定角色
ROLE_RULES = (
    ("/api/platform/", {"admin"}),
    ("/api/merchant/", {"merchant"}),
)
DEFAULT_ROLES = {"customer", "admin"}


def _public_read_paths():
    """允许额外放行的只读路径（默认空 = 严格模式，游客看不到任何业务数据）。

    若想让游客能浏览商品，在 config.json 里写：
        "public_read_paths": ["/api/products"]
    """
    val = app_config.get("access.public_read_paths", []) or []
    if isinstance(val, str):
        val = [x.strip() for x in val.split(",") if x.strip()]
    return tuple(str(x) for x in val if x)


def _is_public(path):
    if path in PUBLIC_API:
        return True
    for prefix in _public_read_paths():
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def allowed_roles(path):
    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in ANY_ROLE_API):
        return {"customer", "merchant", "admin"}
    for prefix, roles in ROLE_RULES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return roles
    return DEFAULT_ROLES


# ---------------------------------------------------------------- 身份解析

_ID_CACHE = {}
_ID_TTL = max(0, int(_DEFAULTS["identity_cache_seconds"]))
_lock = threading.Lock()


def _raw_token():
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Platform-Token")
            or request.headers.get("X-Merchant-Token")
            or request.args.get("token")
            or "").strip()


def resolve_identity():
    """把请求里的令牌解析成统一身份；无效返回 None。

    三类令牌共用 Authorization 头，按格式与来源依次尝试：
      顾客 kb1.<payload>.<sig> -> 商户（MySQL cs.merchant） -> 平台管理员（内存）
    """
    token = _raw_token()
    if not token:
        return None
    now = time.time()
    ttl = cfg("identity_cache_seconds")
    with _lock:
        hit = _ID_CACHE.get(token)
        if hit and hit[1] > now:
            return dict(hit[0])
        if hit:
            _ID_CACHE.pop(token, None)

    info = None
    if token.startswith("kb1."):
        cu = verify_customer_token(token)
        if cu:
            info = {"role": "customer", "id": str(cu["id"]), "name": cu.get("name") or ""}
    else:
        merchant = get_merchant_by_token(token)
        if merchant:
            info = {"role": "merchant", "id": str(merchant["id"]),
                    "name": merchant.get("shop_name") or merchant.get("name") or "",
                    "status": merchant.get("status") or ""}
        else:
            admin = verify_admin_token(token)
            if admin:
                info = {"role": "admin", "id": str(admin.get("id") or "admin"),
                        "name": admin.get("name") or ""}

    if info and ttl > 0:
        with _lock:
            _ID_CACHE[token] = (dict(info), now + ttl)
            if len(_ID_CACHE) > 5000:
                _ID_CACHE.clear()
    return info


def invalidate_identity_cache(token=None):
    with _lock:
        if token:
            _ID_CACHE.pop(token, None)
        else:
            _ID_CACHE.clear()


# ---------------------------------------------------------------- 次数限制

_RATE = defaultdict(deque)


def _hit(key, limit, window):
    """滑动窗口计数。返回 (是否放行, 还需等待秒数)。"""
    if limit <= 0:
        return True, 0
    now = time.time()
    with _lock:
        q = _RATE[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(q[0] + window - now) + 1)
        q.append(now)
        if len(_RATE) > 20000:
            for k in [k for k, v in _RATE.items() if not v or v[-1] < now - 3600]:
                _RATE.pop(k, None)
    return True, 0


def check_limits(identity, path):
    """返回 (是否放行, 提示, 等待秒数)。"""
    ip = client_ip() or "unknown"
    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in LOGIN_API):
        ok, wait = _hit("login:" + ip, cfg("login_per_minute"), 60)
        if not ok:
            return False, "尝试过于频繁，请稍后再试", wait
    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in REGISTER_API):
        ok, wait = _hit("register:" + ip, cfg("register_per_hour"), 3600)
        if not ok:
            return False, "注册 / 验证码请求过于频繁，请稍后再试", wait

    if not identity:
        return True, "", 0
    actor = identity["role"] + ":" + identity["id"]
    ok, wait = _hit("general:" + actor, cfg("general_per_minute"), 60)
    if not ok:
        return False, "操作过于频繁，请稍后再试", wait

    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in CHAT_API):
        day = time.strftime("%Y%m%d")
        ok, wait = _hit("chat:" + actor + ":" + day, cfg("chat_daily_quota"), 86400)
        if not ok:
            return False, "今日客服调用次数已用完，请明天再试", wait
    return True, "", 0


# ---------------------------------------------------------------- 操作留痕

SENSITIVE_KEYS = frozenset((
    "password", "password2", "pwd", "old_password", "new_password",
    "token", "code", "verify_code", "email_code", "captcha",
    "api_key", "secret", "authorization", "platform_token", "merchant_token",
))


def _mask(obj, depth=0):
    if depth > 6:
        return "..."
    if isinstance(obj, dict):
        return {str(k): ("***" if str(k).lower() in SENSITIVE_KEYS else _mask(v, depth + 1))
                for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return [_mask(x, depth + 1) for x in obj[:20]]
    if isinstance(obj, str) and len(obj) > 200:
        return obj[:200] + "…"
    return obj


def _digest(value, limit):
    try:
        return json.dumps(_mask(value), ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return ""


def _body_digest():
    if "multipart" in (request.mimetype or ""):
        return "<multipart>"
    raw = request.get_data(cache=True, as_text=True) or ""
    if not raw:
        return ""
    try:
        return _digest(json.loads(raw), 1000)
    except (ValueError, TypeError):
        return raw[:300]


def _actor_hint():
    """登录 / 注册请求尚无令牌，从请求体里取一个非敏感的用户名作留痕线索。"""
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("username") or data.get("name") or "")[:64]


CREATE_AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS `audit_log` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT,
    `ts`          DATETIME(3)  NOT NULL,
    `actor_role`  VARCHAR(16)  DEFAULT NULL,
    `actor_id`    VARCHAR(64)  DEFAULT NULL,
    `actor_name`  VARCHAR(128) DEFAULT NULL,
    `method`      VARCHAR(8)   DEFAULT NULL,
    `path`        VARCHAR(255) DEFAULT NULL,
    `query`       TEXT,
    `body`        TEXT,
    `status`      SMALLINT     DEFAULT NULL,
    `ip`          VARCHAR(64)  DEFAULT NULL,
    `duration_ms` INT          DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_actor` (`actor_id`, `ts`),
    KEY `idx_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全站操作留痕';
"""


def ensure_audit_table():
    """启动时建表；失败只告警，不阻断服务。"""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(CREATE_AUDIT_SQL)
        conn.commit()
        conn.close()
        return True, "audit_log 表已就绪"
    except Exception as e:
        print(f"[access] 操作留痕表初始化失败（不影响启动）：{e}")
        return False, str(e)


def write_audit(status):
    if not cfg("audit_enabled"):
        return
    identity = getattr(g, "access_identity", None)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `audit_log` "
                "(`ts`,`actor_role`,`actor_id`,`actor_name`,`method`,`path`,"
                " `query`,`body`,`status`,`ip`,`duration_ms`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    datetime.now(),
                    identity["role"] if identity else "anonymous",
                    identity["id"] if identity else None,
                    (identity["name"] if identity else "")[:128] or _actor_hint() or None,
                    request.method,
                    request.path[:255],
                    _digest(request.args.to_dict(), 500),
                    _body_digest(),
                    int(status),
                    (client_ip() or "")[:64],
                    int((time.time() - getattr(g, "access_started", time.time())) * 1000),
                ),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[access] 操作留痕写入失败（不影响请求）：{e}")


def query_audit_logs(limit=200, role=None, actor_id=None, path=None):
    """供平台管理端查看留痕。"""
    where, args = [], []
    if role:
        where.append("`actor_role`=%s")
        args.append(role)
    if actor_id:
        where.append("`actor_id`=%s")
        args.append(str(actor_id))
    if path:
        where.append("`path` LIKE %s")
        args.append("%" + str(path) + "%")
    sql = "SELECT * FROM `audit_log`"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY `id` DESC LIMIT %s"
    args.append(min(int(limit or 200), 1000))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        for r in rows:
            if r.get("ts"):
                r["ts"] = r["ts"].strftime("%Y-%m-%d %H:%M:%S")
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------- Flask 钩子

def before():
    """准入检查：登录 -> 角色 -> 次数。返回响应即拦截。"""
    g.access_started = time.time()
    g.access_identity = None
    path = request.path
    if request.method == "OPTIONS" or not path.startswith("/api/"):
        return None

    identity = resolve_identity()
    g.access_identity = identity

    if not is_enabled():
        return None

    public = _is_public(path)
    if not public and identity is None:
        resp = jsonify({"ok": False, "code": "NO_AUTH",
                        "error": "请先登录后再使用"})
        resp.status_code = 401
        return resp

    if identity:
        roles = allowed_roles(path)
        if identity["role"] not in roles and not public:
            resp = jsonify({"ok": False, "code": "FORBIDDEN",
                            "error": "当前账号无权访问该功能"})
            resp.status_code = 403
            return resp
        # 商户必须已通过平台审核（登录接口本身在白名单里，不受此限制）
        if identity["role"] == "merchant" and identity.get("status") not in ("approved", ""):
            resp = jsonify({"ok": False, "code": "PENDING_REVIEW",
                            "error": "账号尚未通过平台审核"})
            resp.status_code = 403
            return resp

    ok, msg, wait = check_limits(identity, path)
    if not ok:
        resp = jsonify({"ok": False, "code": "RATE_LIMITED", "error": msg,
                        "retry_after": wait})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp
    return None


def after(resp):
    """留痕：所有 /api/* 请求，包含被准入钩子拒绝的请求。"""
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return resp
    write_audit(resp.status_code)
    return resp
