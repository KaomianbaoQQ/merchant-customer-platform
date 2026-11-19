"""平台（管理员）对商家 / 商品的管控状态。

存一份本地 JSON（商户界面/platform_state.json），记录：
    merchants: {商户ID: {"disabled": 是否停用,
                        "muted_until": 禁言到期时间(YYYY-MM-DD HH:MM:SS，空=未禁言),
                        "muted_days": 禁言天数,
                        "disabled_offline_pids": [停用时刻在售、被平台下架的商品ID]}}
    products:  {商品ID: {"offline": 是否下架, "reason": 下架原因, "time": 操作时间}}
    fines:     [{"merchant_id", "merchant_name", "amount", "reason", "time"}]
    balances:  {商户ID: {"balance": 余额, "total_revenue": 累计入账, "total_fine": 累计罚款}}
    ledger:    [{"merchant_id", "type": "revenue"|"fine", "amount", "ref", "note", "time"}]

客户后端（后端 Flask）与商户后台共用本模块：平台停用商家后商家登录被拒、
禁言后不能回复顾客（按天计到期自动解禁）、下架的商品不再对顾客展示、
停用商家时其全部在售商品被平台下架、小店余额随订单入账与罚款扣出。
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

DEFAULT = {"merchants": {}, "products": {}, "fines": [], "balances": {}, "ledger": []}

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
    if not isinstance(data.get("balances"), dict):
        data["balances"] = {}
    if not isinstance(data.get("ledger"), list):
        data["ledger"] = []
    return data

def _save(data):
    os.makedirs(MERCHANT_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

# 商家：停用 / 禁言 / 罚款 / 余额
_SECONDS_PER_DAY = 86400

def merchant_state(merchant_id):
    """返回商家管控状态。

    返回 {"disabled": bool, "muted": bool(按到期时间计算的有效禁言),
          "muted_until": str, "muted_days": int, "mute_remaining_days": int}
    """
    mid = str(merchant_id or "").strip()
    data = _load()
    item = data["merchants"].get(mid) or {}
    disabled = bool(item.get("disabled"))
    muted_until = str(item.get("muted_until") or "").strip()
    muted_days = int(item.get("muted_days") or 0)
    muted = False
    remaining = 0
    if muted_until:
        try:
            end = time.mktime(time.strptime(muted_until, "%Y-%m-%d %H:%M:%S"))
            diff = end - time.time()
            # 向上取整：刚禁言 N 天即显示 N 天，跨过到期时刻才归 0
            remaining = -(-int(diff) // _SECONDS_PER_DAY) if diff > 0 else 0
            if remaining < 0:
                remaining = 0
            muted = time.time() < end
        except (ValueError, OverflowError):
            muted = False
            remaining = 0
    return {
        "disabled": disabled,
        "muted": muted,
        "muted_until": muted_until,
        "muted_days": muted_days,
        "mute_remaining_days": remaining,
        "disabled_offline_pids": list(item.get("disabled_offline_pids") or []),
    }

def is_merchant_disabled(merchant_id):
    return merchant_state(merchant_id)["disabled"]

def is_merchant_muted(merchant_id):
    return merchant_state(merchant_id)["muted"]

def mute_remaining_days(merchant_id):
    return merchant_state(merchant_id)["mute_remaining_days"]

def set_merchant_flag(merchant_id, key, value, days=0):
    """设置商家的 disabled / muted 标记。

    - disabled: value 为 True/False。
    - muted:    value 为真时按 days 天禁言（days<=0 视为 1 天）；value 为假则解除禁言。
    返回设置后的状态 dict。
    """
    if key == "disabled":
        return set_merchant_disabled(merchant_id, bool(value))
    if key == "muted":
        return set_merchant_muted(merchant_id, int(days) if value else 0)
    raise ValueError("不支持的商家标记：" + str(key))

def set_merchant_disabled(merchant_id, value, offline_pids=None):
    """停用 / 恢复商家账号。返回设置后的状态 dict。

    offline_pids 仅在 value=True 时生效：记录「停用时刻在售、被平台下架」的商品 ID，
    便于恢复账号时把这批商品恢复上架（其它原本就下架的商品不受影响）。
    """
    mid = str(merchant_id or "").strip()
    with _locked():
        data = _load()
        item = data["merchants"].setdefault(mid, {})
        item["disabled"] = bool(value)
        if value:
            item["disabled_offline_pids"] = list(offline_pids or [])
        else:
            item["disabled_offline_pids"] = []
        item["update_time"] = _now()
        _save(data)
        return merchant_state(mid)

def set_merchant_muted(merchant_id, days):
    """按天禁言 / 解除禁言。days>0 时禁言 days 天；days<=0 解除。返回状态 dict。"""
    mid = str(merchant_id or "").strip()
    days = int(days or 0)
    with _locked():
        data = _load()
        item = data["merchants"].setdefault(mid, {})
        if days > 0:
            item["muted_until"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + days * _SECONDS_PER_DAY)
            )
            item["muted_days"] = days
        else:
            item["muted_until"] = ""
            item["muted_days"] = 0
        item["update_time"] = _now()
        _save(data)
        return merchant_state(mid)

def add_fine(merchant_id, merchant_name, amount, reason):
    """给商家记一笔罚款，并从余额中扣出。返回 (是否成功, 提示, 罚款记录)。"""
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
    # 罚款从余额扣出（与 fines 解耦，余额可单独查询）
    deduct_fine(merchant_id, round(amount, 2), reason)
    return True, f"已对「{entry['merchant_name'] or entry['merchant_id']}」罚款 {entry['amount']} 元（已从余额扣出）", entry

# 余额：入账（订单完成）/ 扣出（罚款）/ 流水
def _balance_item(data, mid):
    item = data["balances"].get(mid)
    if not isinstance(item, dict):
        item = {"balance": 0.0, "total_revenue": 0.0, "total_fine": 0.0}
        data["balances"][mid] = item
    for k in ("balance", "total_revenue", "total_fine"):
        try:
            item[k] = float(item.get(k) or 0)
        except (TypeError, ValueError):
            item[k] = 0.0
    return item

def get_balance(merchant_id):
    """返回 {'balance','total_revenue','total_fine'}（浮点，缺省为 0）。"""
    mid = str(merchant_id or "").strip()
    data = _load()
    item = _balance_item(data, mid)
    return {
        "balance": round(item["balance"], 2),
        "total_revenue": round(item["total_revenue"], 2),
        "total_fine": round(item["total_fine"], 2),
    }

def credit_revenue(merchant_id, amount, ref="", note=""):
    """订单完成入账：增加余额与累计入账，并写一笔流水。返回最新余额 dict。"""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return get_balance(merchant_id)
    if amount <= 0:
        return get_balance(merchant_id)
    mid = str(merchant_id or "").strip()
    with _locked():
        data = _load()
        item = _balance_item(data, mid)
        item["balance"] = round(item["balance"] + amount, 2)
        item["total_revenue"] = round(item["total_revenue"] + amount, 2)
        data["ledger"].append({
            "merchant_id": mid,
            "type": "revenue",
            "amount": round(amount, 2),
            "ref": str(ref or "").strip(),
            "note": str(note or "").strip(),
            "time": _now(),
        })
        _save(data)
        return get_balance(mid)

def deduct_fine(merchant_id, amount, reason=""):
    """罚款扣出：减少余额与增加累计罚款，并写一笔流水。返回最新余额 dict。"""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return get_balance(merchant_id)
    if amount <= 0:
        return get_balance(merchant_id)
    mid = str(merchant_id or "").strip()
    with _locked():
        data = _load()
        item = _balance_item(data, mid)
        item["balance"] = round(item["balance"] - amount, 2)
        item["total_fine"] = round(item["total_fine"] + amount, 2)
        data["ledger"].append({
            "merchant_id": mid,
            "type": "fine",
            "amount": round(amount, 2),
            "ref": "",
            "note": str(reason or "").strip(),
            "time": _now(),
        })
        _save(data)
        return get_balance(mid)

def list_ledger(merchant_id=None):
    """余额流水（最新在前）。可按商家过滤。"""
    mid = str(merchant_id or "").strip()
    data = _load()
    items = [x for x in data.get("ledger") or []
             if not mid or str(x.get("merchant_id")) == mid]
    items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return items

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