"""平台（管理员）对商家 / 商品的管控状态。

存一份本地 JSON（商户界面/platform_state.json），记录：
    merchants: {商户ID: {"disabled": 是否停用, "muted": 是否禁言}}
    products:  {商品ID: {"offline": 是否下架, "reason": 下架原因, "time": 操作时间}}
    fines:     [{"merchant_id", "merchant_name", "amount", "reason", "time"}]

客户后端（后端 Flask）与商户后台共用本模块：平台停用商家后商家登录被拒、
禁言后不能回复顾客、下架的商品不再对顾客展示。
仅依赖标准库。
"""
import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERCHANT_DIR = os.path.join(ROOT_DIR, "商户界面")
STATE_FILE = os.path.join(MERCHANT_DIR, "platform_state.json")

DEFAULT = {"merchants": {}, "products": {}, "fines": []}

_thread_lock = threading.RLock()

@contextmanager
def _locked():
    with _thread_lock:
        os.makedirs(MERCHANT_DIR, exist_ok=True)
        lock_path = os.path.join(MERCHANT_DIR, ".platform_state.lock")
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

def _load():
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT))
    for key in ("merchants", "products"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    if not isinstance(data.get("fines"), list):
        data["fines"] = []
    return data

def _save(data):
    os.makedirs(MERCHANT_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

# 商家：停用 / 禁言 / 罚款
def merchant_state(merchant_id):
    """返回 {"disabled": bool, "muted": bool}。"""
    mid = str(merchant_id or "").strip()
    data = _load()
    item = data["merchants"].get(mid) or {}
    return {"disabled": bool(item.get("disabled")), "muted": bool(item.get("muted"))}

def is_merchant_disabled(merchant_id):
    return merchant_state(merchant_id)["disabled"]

def is_merchant_muted(merchant_id):
    return merchant_state(merchant_id)["muted"]

def set_merchant_flag(merchant_id, key, value):
    """设置商家的 disabled / muted 标记。返回设置后的状态 dict。"""
    if key not in ("disabled", "muted"):
        raise ValueError("不支持的商家标记：" + str(key))
    mid = str(merchant_id or "").strip()
    with _locked():
        data = _load()
        item = data["merchants"].setdefault(mid, {})
        item[key] = bool(value)
        item["update_time"] = _now()
        _save(data)
        return {"disabled": bool(item.get("disabled")), "muted": bool(item.get("muted"))}

def add_fine(merchant_id, merchant_name, amount, reason):
    """给商家记一笔罚款。返回 (是否成功, 提示, 罚款记录)。"""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "罚款金额必须是数字", None
    if amount <= 0:
        return False, "罚款金额必须大于 0", None
    reason = str(reason or "").strip()
    if not reason:
        return False, "请填写罚款原因", None

    entry = {
        "merchant_id": str(merchant_id or "").strip(),
        "merchant_name": str(merchant_name or "").strip(),
        "amount": round(amount, 2),
        "reason": reason,
        "time": _now(),
    }
    with _locked():
        data = _load()
        data["fines"].append(entry)
        _save(data)
    return True, f"已对「{entry['merchant_name'] or entry['merchant_id']}」罚款 {entry['amount']} 元", entry

def list_fines(merchant_id=None):
    """罚款记录，可按商家过滤（最新在前）。"""
    mid = str(merchant_id or "").strip()
    data = _load()
    items = [f for f in data["fines"] if not mid or str(f.get("merchant_id")) == mid]
    items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return items

def fine_total(merchant_id):
    return round(sum(float(f.get("amount") or 0) for f in list_fines(merchant_id)), 2)

# 商品：下架 / 恢复
def product_state(product_id):
    """返回 {"offline": bool, "reason": str, "time": str}。"""
    pid = str(product_id or "").strip()
    data = _load()
    item = data["products"].get(pid) or {}
    return {
        "offline": bool(item.get("offline")),
        "reason": item.get("reason") or "",
        "time": item.get("time") or "",
    }

def is_product_offline(product_id):
    return product_state(product_id)["offline"]

def set_product_offline(product_id, offline, reason=""):
    """平台下架 / 恢复商品。返回设置后的状态 dict。"""
    pid = str(product_id or "").strip()
    if not pid:
        raise ValueError("缺少商品ID")
    with _locked():
        data = _load()
        if offline:
            data["products"][pid] = {
                "offline": True,
                "reason": str(reason or "").strip(),
                "time": _now(),
            }
        else:
            data["products"].pop(pid, None)
        _save(data)
    return product_state(pid)

def offline_product_ids():
    data = _load()
    return {pid for pid, item in data["products"].items() if item.get("offline")}