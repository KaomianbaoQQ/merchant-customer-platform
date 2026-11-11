"""平台管理员令牌：管理员身份的唯一凭据。

原先判定管理员是「前端传的 user_id + user_name 命中 admin.json」，
但 user_id / user_name 是前端随便填的，任何人带上
`user_id=admin&user_name=admin` 就能拿到管理员权限（能看全部订单、
绕过退换货的归属校验）。现改为：只有 /api/platform/login 用密码换取的
令牌才是管理员身份，令牌不存在则一律按普通用户处理。

令牌存在进程内存里（后端重启后需重新登录），仅标准库。
"""
import secrets
import threading

_lock = threading.Lock()
# token -> {"id", "name", "role"}
_tokens = {}


def issue_token(admin):
    """给校验通过的管理员签发一个新令牌。"""
    token = secrets.token_hex(16)
    with _lock:
        _tokens[token] = {
            "id": str(admin.get("id") or "admin"),
            "name": str(admin.get("name") or "admin"),
            "role": str(admin.get("role") or "平台管理员"),
        }
    return token


def verify_token(token):
    """校验令牌，返回管理员信息 dict；无效返回 None。"""
    token = str(token or "").strip()
    if not token:
        return None
    with _lock:
        info = _tokens.get(token)
        return dict(info) if info else None


def revoke_token(token):
    """退出登录时吊销令牌。"""
    token = str(token or "").strip()
    if not token:
        return False
    with _lock:
        return _tokens.pop(token, None) is not None