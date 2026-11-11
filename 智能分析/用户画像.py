"""用户画像：把「订单 + 评价 + 行为埋点 + 商品」汇总成人能看懂的顾客画像。

画像维度：
    RFM        最近购买（Recency）/ 购买频次（Frequency）/ 消费金额（Monetary）
    客户分层    按 RFM 打分归到「重要价值 / 重要保持 / 重要发展 / 重要挽留 / 一般 / 流失」
    行为统计    浏览 / 加购 / 购买 次数（来自 公共/behavior.py 埋点）
    品类偏好    买得最多的商品与类目（tag）
    标签        新客 / 活跃 / 高价值 / 犹豫未购 / 兴趣浓厚 / 流失风险 / 复购达人 …

对外：
    list_profiles()          全部顾客画像（管理员用）
    build_profile(uid)       单个顾客画像（含订单明细，详情页用）
    profiles_by_merchant(mid) 只统计在指定商家下过单 / 有行为的顾客（商户用）

只依赖标准库 + 项目内模块，无需额外依赖。
"""
import time
from datetime import datetime

from 公共 import store
from 公共 import behavior
from 公共.db_config import get_conn

# RFM 打分阈值（可按业务调整）
_R_DAYS = [(7, 5), (14, 4), (30, 3), (60, 2)]      # 距今 <=N 天 → 分数
_F_COUNT = [(1, 2), (3, 3), (5, 4)]                # 订单数 <=N → 分数（>=6 给 5）
_M_AMOUNT = [(300, 2), (1000, 3), (3000, 4)]       # 消费额 <N → 分数（>=3000 给 5）

# 数据读取（MySQL + products.json + Mongo 埋点）
def _safe(sql, params=None):
    try:
        conn = get_conn()
    except Exception:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall() or []
    except Exception:
        return []
    finally:
        conn.close()

def _users():
    return _safe("SELECT id, name, age, sex FROM `user` ORDER BY id")

def _orders():
    return _safe(
        "SELECT id, product_id, goodsname, quantity, price, user_id, name, merchant_id, "
        "merchant_name, create_time FROM `order` ORDER BY create_time DESC"
    )

def _reviews():
    return _safe("SELECT product_id, user_id, user_name, rating FROM `review`")

def _products():
    return store.list_products(include_offline=True)

def _amount(order):
    try:
        return round(float(order.get("price") or 0) * int(order.get("quantity") or 0), 2)
    except (TypeError, ValueError):
        return 0.0

def _days_since(value):
    """距今多少天；value 是 datetime / 字符串，解析失败返回 None。"""
    if value is None:
        return None
    dt = value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    try:
        return max(0, int((datetime.now() - dt).total_seconds() // 86400))
    except Exception:
        return None

def _score(value, table, ceil=None):
    """按阈值表打分；value 为 None 返回 0。ceil 命中即满分。"""
    if value is None:
        return 0
    for bound, score in table:
        if value <= bound:
            return score
    return ceil if ceil is not None else len(table) + 1

def _level(r, f, m):
    """按 RFM 分数归客户分层。"""
    if not any((r, f, m)):
        return "新客（未下单）"
    if r >= 4 and f >= 3 and m >= 3:
        return "重要价值客户"
    if r <= 2 and f >= 3:
        return "重要保持客户"
    if r >= 4 and f <= 2:
        return "重要发展客户"
    if m >= 4 and r <= 2:
        return "重要挽留客户"
    if r <= 2:
        return "一般保持客户"
    return "流失预警客户"

def _load_ctx():
    """一次性把数据读出来，避免每算一个用户就打一轮库。"""
    products = _products()
    by_id = {str(p.get("id")): p for p in products if p.get("id")}
    by_name = {}
    try:
        from 售后.order import base_goodsname
    except Exception:
        base_goodsname = lambda s: str(s or "")
    for p in products:
        if p.get("name"):
            by_name.setdefault(base_goodsname(p.get("name")), p)

    orders_by_user, orders_by_merchant_user = {}, {}
    for o in _orders():
        uid = str(o.get("user_id") or "")
        orders_by_user.setdefault(uid, []).append(o)
        mid = str(o.get("merchant_id") or "")
        if uid and mid:
            orders_by_merchant_user.setdefault(mid, set()).add(uid)

    reviews_by_product = {}
    for r in _reviews():
        pid = str(r.get("product_id") or "")
        lst = reviews_by_product.setdefault(pid, [])
        lst.append(int(r.get("rating") or 0))

    return {
        "products": products,
        "pid": by_id,
        "pname": by_name,
        "orders_by_user": orders_by_user,
        "orders_by_merchant_user": orders_by_merchant_user,
        "reviews_by_product": reviews_by_product,
        "beh": behavior.counts_by_user(),
        "beh_matrix": behavior.matrix(),
    }

def _match_product(order, ctx):
    """把一条订单对应到商品字典（优先 product_id，其次商品名兜底）。"""
    pid = str(order.get("product_id") or "")
    if pid in ctx["pid"]:
        return ctx["pid"][pid]
    try:
        from 售后.order import base_goodsname
        name = base_goodsname(order.get("goodsname"))
    except Exception:
        name = str(order.get("goodsname") or "")
    return ctx["pname"].get(name)

# 单个画像
def _profile_of(user, ctx):
    uid = str(user.get("id"))
    orders = ctx["orders_by_user"].get(uid, [])
    amount = round(sum(_amount(o) for o in orders), 2)
    qty = sum(int(o.get("quantity") or 0) for o in orders)
    last_days = _days_since(orders[0].get("create_time")) if orders else None

    r = _score(last_days, _R_DAYS, ceil=5) if orders else 0
    f = _score(len(orders), _F_COUNT, ceil=5) if orders else 0
    m = _score(amount, _M_AMOUNT, ceil=5) if orders else 0

    # 品类 / 商品偏好
    tag_count, prod_count, bought_pids = {}, {}, set()
    for o in orders:
        p = _match_product(o, ctx)
        pid = str((p or {}).get("id") or o.get("product_id") or "")
        if pid:
            bought_pids.add(pid)
        name = (p or {}).get("name") or o.get("goodsname") or ""
        if name:
            prod_count[name] = prod_count.get(name, 0) + int(o.get("quantity") or 0)
        tag = (p or {}).get("tag") or "未分类"
        tag_count[tag] = tag_count.get(tag, 0) + int(o.get("quantity") or 0)

    top_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)
    top_products = sorted(prod_count.items(), key=lambda x: x[1], reverse=True)

    beh = ctx["beh"].get(uid) or {"view": 0, "cart": 0, "purchase": 0}
    # 该用户浏览/加购过、但从未买过的商品数（衡量「犹豫」）
    hesitant = 0
    for (u, pid), c in ctx["beh_matrix"].items():
        if u != uid:
            continue
        if (c.get("view") or c.get("cart")) and pid not in bought_pids:
            hesitant += 1

    repurchase = round(1 - len(bought_pids) / len(orders), 2) if orders else 0.0

    # 兴趣度：浏览 + 加购行为强度（0~5）
    interest = min(5, (beh["view"] + beh["cart"] * 2) / 4)

    tags = []
    if not orders:
        tags.append("新客（未下单）")
    if len(orders) >= 2:
        tags.append("活跃")
    if amount >= 1000:
        tags.append("高价值")
    if repurchase >= 0.5 and len(orders) >= 3:
        tags.append("复购达人")
    if beh["cart"] and beh["purchase"] == 0:
        tags.append("犹豫未购")
    if beh["view"] >= 5:
        tags.append("兴趣浓厚")
    if orders and last_days is not None and last_days > 30:
        tags.append("流失风险")
    if top_tags:
        tags.append("偏好：" + top_tags[0][0])

    return {
        "id": uid,
        "name": user.get("name") or "",
        "sex": user.get("sex") or "未知",
        "age": user.get("age"),
        "order_count": len(orders),
        "quantity": qty,
        "amount": amount,
        "last_order_days": last_days,
        "avg_price": round(amount / qty, 2) if qty else 0,
        "rfm": {"r": r, "f": f, "m": m},
        "level": _level(r, f, m),
        "behavior": {"views": beh["view"], "carts": beh["cart"], "purchases": beh["purchase"]},
        "interest": round(interest, 1),
        "repurchase": repurchase,
        "hesitant_products": hesitant,
        "top_tags": [{"tag": t, "qty": n} for t, n in top_tags[:5]],
        "top_products": [{"name": n, "qty": c} for n, c in top_products[:5]],
        "tags": tags,
    }

def list_profiles():
    """全部顾客画像，按消费额倒序。"""
    ctx = _load_ctx()
    rows = [_profile_of(u, ctx) for u in _users()]
    rows.sort(key=lambda x: x["amount"], reverse=True)
    return rows

def build_profile(uid):
    """单个顾客画像（含订单明细与服务记录数），不存在返回 None。"""
    uid = str(uid or "").strip()
    ctx = _load_ctx()
    user = next((u for u in _users() if str(u.get("id")) == uid), None)
    if not user:
        return None
    profile = _profile_of(user, ctx)
    orders = ctx["orders_by_user"].get(uid, [])
    detail = []
    for o in orders:
        p = _match_product(o, ctx) or {}
        detail.append({
            "order_id": o.get("id"),
            "product_id": o.get("product_id") or "",
            "goodsname": p.get("name") or o.get("goodsname") or "",
            "quantity": int(o.get("quantity") or 0),
            "price": o.get("price"),
            "merchant_name": o.get("merchant_name") or "",
            "create_time": str(o.get("create_time") or ""),
        })
    profile["orders"] = detail
    return profile

def profiles_by_merchant(mid):
    """只在指定商家下过单 / 有行为的顾客（商户「顾客洞察」用）。"""
    mid = str(mid or "").strip()
    ctx = _load_ctx()
    uids = set(ctx["orders_by_merchant_user"].get(mid) or set())
    # 在该商家商品上有浏览 / 加购行为的顾客也算
    own_pids = {str(p.get("id")) for p in ctx["products"]
                if str(p.get("owner_id") or "") == mid}
    for (u, pid) in ctx["beh_matrix"]:
        if pid in own_pids:
            uids.add(u)
    rows = [_profile_of(u, ctx) for u in _users() if str(u.get("id")) in uids]
    rows.sort(key=lambda x: x["amount"], reverse=True)
    return rows