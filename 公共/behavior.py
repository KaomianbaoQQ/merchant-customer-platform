"""用户行为埋点（浏览 / 加购 / 下单）的 NoSQL 存储（MongoDB）。

供「智能分析」模块计算用户画像与商品购买可能性使用：
    view      顾客打开商品详情（浏览）
    cart      顾客把商品加入购物袋（加购）
    purchase  顾客成功下单（购买）

每条行为一个文档（存在 MongoDB，与聊天记录同一个库）：
    {user_id, user_name, event, product_id, product_name, merchant_id, ts, time, extra}

降级策略与聊天记录（公共/chat_store.py）一致：MongoDB 连不上时只打印中文告警，
返回空结果，**不影响下单 / 浏览 / 客服主流程**。
"""
import time

from 公共.chat_store import get_client
from 公共.db_config import MONGO_CONFIG

BEHAVIOR_COLLECTION = "user_behaviors"
EVENTS = ("view", "cart", "purchase")
_warned = False

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _coll():
    return get_client()[MONGO_CONFIG["database"]][BEHAVIOR_COLLECTION]

def _warn(error):
    """连接失败只告警一次，别刷屏。"""
    global _warned
    if not _warned:
        _warned = True
        print(f"[行为埋点] MongoDB 不可用，行为数据暂不记录：{str(error).split(', Topology')[0]}")

def ensure_indexes():
    """建索引（幂等）。返回 (是否成功, 提示信息)，供启动自检调用。"""
    try:
        _coll().create_index([("user_id", 1), ("ts", -1)])
        _coll().create_index([("product_id", 1), ("ts", -1)])
        _coll().create_index([("event", 1), ("ts", -1)])
        return True, "用户行为集合已就绪（MongoDB）"
    except Exception as e:
        return False, f"MongoDB 连接失败：{e}"

def record(user_id, event, product_id="", product_name="", merchant_id="",
           user_name="", extra=None):
    """记录一条行为。event 只认 view / cart / purchase，其余忽略。失败返回 False。"""
    event = str(event or "").strip()
    if event not in EVENTS:
        return False
    user_id = str(user_id or "").strip()
    product_id = str(product_id or "").strip()
    if not user_id and not product_id:
        return False
    doc = {
        "user_id": user_id,
        "user_name": str(user_name or ""),
        "event": event,
        "product_id": product_id,
        "product_name": str(product_name or ""),
        "merchant_id": str(merchant_id or ""),
        "ts": time.time(),
        "time": _now(),
        "extra": extra if isinstance(extra, dict) else {},
    }
    try:
        _coll().insert_one(doc)
        return True
    except Exception as e:
        _warn(e)
        return False

def _query(event=None, user_id=None, product_id=None, since_ts=None, until_ts=None):
    q = {}
    if event:
        q["event"] = event
    if user_id:
        q["user_id"] = str(user_id)
    if product_id:
        q["product_id"] = str(product_id)
    if since_ts is not None or until_ts is not None:
        rng = {}
        if since_ts is not None:
            rng["$gte"] = since_ts
        if until_ts is not None:
            rng["$lte"] = until_ts
        q["ts"] = rng
    return q

def list_by_user(user_id, event=None, limit=1000):
    """某个用户的全部行为（按时间倒序）。"""
    try:
        cur = _coll().find(_query(event=event, user_id=user_id)).sort("ts", -1).limit(limit)
        return list(cur)
    except Exception as e:
        _warn(e)
        return []

def list_all(event=None, limit=5000):
    """全部行为（按时间倒序），供平台聚合。"""
    try:
        cur = _coll().find(_query(event=event)).sort("ts", -1).limit(limit)
        return list(cur)
    except Exception as e:
        _warn(e)
        return []

def _counts(rows, field):
    """把行为流水按 field（user_id / product_id）压成计数。"""
    out = {}
    for r in rows:
        key = str(r.get(field) or "")
        if not key:
            continue
        item = out.setdefault(key, {"view": 0, "cart": 0, "purchase": 0})
        ev = str(r.get("event") or "")
        if ev in item:
            item[ev] += 1
    return out

def counts_by_user():
    """{user_id: {"view": n, "cart": n, "purchase": n}}。"""
    return _counts(list_all(), "user_id")

def counts_by_product():
    """{product_id: {"view": n, "cart": n, "purchase": n}}。"""
    return _counts(list_all(), "product_id")

def matrix():
    """(user_id, product_id) → {"view": n, "cart": n, "purchase": n}，购买预测用。"""
    out = {}
    for r in list_all():
        uid = str(r.get("user_id") or "")
        pid = str(r.get("product_id") or "")
        if not uid or not pid:
            continue
        item = out.setdefault((uid, pid), {"view": 0, "cart": 0, "purchase": 0})
        ev = str(r.get("event") or "")
        if ev in item:
            item[ev] += 1
    return out