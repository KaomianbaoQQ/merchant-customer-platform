"""顾客令牌：HMAC 签名的无状态令牌（后端重启后依然有效）。

格式：``kb1.<payload_b64url>.<sig>``，payload 为
``{"id","name","role","age","sex","iat"}``，签名用 HMAC-SHA256。

签名密钥取值顺序：
    1) 环境变量 ``KB_TOKEN_SECRET``
    2) ``config.json`` → ``security.token_secret``
    3) 都没有时随机生成并写回 ``config.json``（该文件不进版本库）

与旧实现的区别：旧版把令牌存在进程内存里，后端一重启所有已登录用户全被登出。
现在令牌自带签名，重启不影响；校验时额外确认 ``cs.user`` 里仍有该账号，
账号被删掉后旧令牌立即失效。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

TOKEN_TTL = 7 * 24 * 3600
_PREFIX = "kb1"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG_PATH = os.path.join(_ROOT, "config.json")

_lock = threading.Lock()
_secret_cache = None
_revoked = set()


def _read_cfg():
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _persist_secret(secret):
    data = _read_cfg()
    sec = data.get("security")
    if not isinstance(sec, dict):
        sec = {}
        data["security"] = sec
    sec["token_secret"] = secret
    try:
        tmp = _CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _CFG_PATH)
    except OSError:
        pass


def _secret():
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    env = (os.environ.get("KB_TOKEN_SECRET") or "").strip()
    if env:
        _secret_cache = env.encode()
        return _secret_cache
    cfg = _read_cfg()
    val = str((cfg.get("security") or {}).get("token_secret") or "").strip()
    if len(val) >= 32:
        _secret_cache = val.encode()
        return _secret_cache
    new = secrets.token_hex(32)
    _persist_secret(new)
    _secret_cache = new.encode()
    return _secret_cache


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body):
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()


def issue_customer_token(user):
    """给校验通过的顾客签发令牌。user 需含 id / name，可选 age / sex。"""
    payload = {
        "id": str(user.get("id") or ""),
        "name": str(user.get("name") or ""),
        "role": "user",
        "age": user.get("age"),
        "sex": user.get("sex"),
        "iat": int(time.time()),
    }
    body = _b64e(json.dumps(payload, ensure_ascii=False,
                            separators=(",", ":")).encode())
    return f"{_PREFIX}.{body}.{_sign(body)}"


def verify_customer_token(token):
    """签名正确、未过期、未被吊销，且账号仍在 user 表 → 返回用户 dict；否则 None。"""
    token = str(token or "").strip()
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    _, body, sig = parts
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() - float(payload.get("iat") or 0) > TOKEN_TTL:
        return None
    with _lock:
        if token in _revoked:
            return None

    try:
        from 售后.user import get_user
        row = get_user(payload.get("id"))
    except Exception:
        row = None
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "name": row.get("name") or payload.get("name") or "",
        "role": "user",
        "age": row.get("age"),
        "sex": row.get("sex"),
    }


def revoke_customer_token(token):
    """退出登录：把令牌记入内存黑名单（客户端同时会丢掉它）。"""
    token = str(token or "").strip()
    if not token:
        return False
    with _lock:
        _revoked.add(token)
        if len(_revoked) > 5000:
            _revoked.clear()
            _revoked.add(token)
    return True
