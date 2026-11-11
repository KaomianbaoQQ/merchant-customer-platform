"""顾客令牌：登录后签发的身份凭据（解决「客户身份仅凭前端 id+name 可伪造」）。

与 公共/admin_auth.py 类似，但面向普通顾客：
  - 令牌 -> {"id", "name", "role", "age", "sex", "issued_at"}
  - 存进程内存（后端重启需重新登录），仅标准库。
前端登录后把令牌存在 localStorage，之后每次请求带 `Authorization: Bearer <token>`。
未带令牌时后端仍兼容旧式「id + name 回数据库核对」，保证历史前端不 break。
"""
import secrets
import threading
import time

_lock = threading.Lock()
_tokens = {}

# 令牌有效期（秒）。演示场景设长一点（7 天），生产可改小。
TOKEN_TTL = 7 * 24 * 3600

def issue_customer_token(user):
    """给校验通过的顾客签发一个新令牌。user 含 id / name / age / sex。"""
    token = secrets.token_hex(20)
    with _lock:
        _tokens[token] = {
            "id": str(user.get("id") or ""),
            "name": str(user.get("name") or ""),
            "role": "user",
            "age": user.get("age"),
            "sex": user.get("sex"),
            "issued_at": time.time(),
        }
    return token

def verify_customer_token(token):
    """校验顾客令牌，有效且未过期则返回用户信息 dict；否则 None。"""
    token = str(token or "").strip()
    if not token:
        return None
    with _lock:
        info = _tokens.get(token)
    if not info:
        return None
    if time.time() - float(info.get("issued_at") or 0) > TOKEN_TTL:
        with _lock:
            _tokens.pop(token, None)
        return None
    return dict(info)

def revoke_customer_token(token):
    """退出登录时吊销令牌。"""
    token = str(token or "").strip()
    if not token:
        return False
    with _lock:
        return _tokens.pop(token, None) is not None
