"""平台（管理员）后台：跨商家 / 顾客的全局视图与管控。

与客户后端、商户后台共用同一份数据（MySQL cs + 公共/store.py 的 JSON），
因此平台看到的就是商家与顾客正在用的真实数据。

提供：
    overview()               平台总览统计
    list_merchants()         商家列表（含管控状态 / 销量 / 罚款 / 评价）
    merchant_detail(mid)     单个商家的全部信息
    list_users()             顾客列表（含消费统计）
    user_detail(uid)         单个顾客的订单与售后
    list_products()          全部商品（含被平台下架的）
    list_all_reviews()       全部商品评价
    list_all_conversations() 全部会话（人机 + 人工）
    platform_thread(...)     平台与商家的一对一会话
    platform_reply(...)      平台以「平台管理员」身份给商家发消息

仅依赖标准库 + 项目内模块。
"""
from 公共 import store
from 公共.db_config import get_conn
from 公共 import platform_state
from 售后.order import get_orders_by_user
from 售后.returns import get_returns_by_user, list_arbitrations

# 平台与商家对话时，商家侧看到的「顾客ID」
PLATFORM_USER_ID = "platform"
PLATFORM_USER_NAME = "平台管理员"

# 基础查询
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

def _all_orders():
    return _safe(
        "SELECT id, product_id, goodsname, quantity, price, name, user_id, address, "
        "waybill, merchant_id, merchant_name, create_time FROM `order` "
        "ORDER BY create_time DESC"
    )

def _amount(order):
    try:
        return float(order.get("price") or 0) * int(order.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0.0

def _all_products():
    return store.list_products(include_offline=True)

def _product_sales(products=None):
    """按商品汇总销量：{product_id: {"sales", "amount", "order_count", "buyers"}}。"""
    products = products if products is not None else _all_products()
    by_id = {str(p.get("id")): str(p.get("id")) for p in products if p.get("id")}
    by_name = {}
    from 售后.order import base_goodsname
    for p in products:
        if p.get("id"):
            by_name[base_goodsname(p.get("name"))] = str(p.get("id"))

    stats = {}
    for o in _all_orders():
        pid = str(o.get("product_id") or "")
        if pid not in by_id:
            pid = by_name.get(base_goodsname(o.get("goodsname"))) or ""
        if not pid:
            continue
        item = stats.setdefault(pid, {
            "sales": 0, "amount": 0.0, "order_count": 0, "buyers": set(),
        })
        qty = int(o.get("quantity") or 0)
        item["sales"] += qty
        item["amount"] = round(item["amount"] + _amount(o), 2)
        item["order_count"] += 1
        item["buyers"].add(str(o.get("user_id") or ""))
    return stats

def _merchant_sales(products=None):
    """按商家汇总销量：{merchant_id: {...}}。订单优先按 merchant_id 归属，老订单按商品归属兜底。"""
    products = products if products is not None else _all_products()
    pid_owner = {str(p.get("id")): str(p.get("owner_id") or "") for p in products if p.get("id")}
    from 售后.order import base_goodsname
    name_owner = {}
    for p in products:
        if p.get("owner_id"):
            name_owner.setdefault(base_goodsname(p.get("name")), str(p["owner_id"]))

    stats = {}
    for o in _all_orders():
        owner = str(o.get("merchant_id") or "")
        if not owner:
            owner = pid_owner.get(str(o.get("product_id") or "")) or \
                name_owner.get(base_goodsname(o.get("goodsname"))) or ""
        if not owner:
            continue
        item = stats.setdefault(owner, {
            "order_count": 0, "sales": 0, "amount": 0.0, "buyers": set(),
        })
        qty = int(o.get("quantity") or 0)
        item["order_count"] += 1
        item["sales"] += qty
        item["amount"] = round(item["amount"] + _amount(o), 2)
        item["buyers"].add(str(o.get("user_id") or ""))
    return stats

def _merchant_rows():
    return _safe(
        "SELECT id, username, name, shop_name, create_time FROM `merchant` ORDER BY id"
    )

def _user_rows():
    return _safe("SELECT id, name, age, sex FROM `user` ORDER BY id")

def _review_rows():
    return _safe(
        "SELECT id, product_id, user_name, user_sex, rating, content, create_time "
        "FROM `review` ORDER BY create_time DESC"
    )

def _fmt(value):
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value or "")

# 平台总览
def overview():
    products = _all_products()
    merchants = _merchant_rows()
    users = _user_rows()
    orders = _all_orders()
    offline = [p for p in products if platform_state.is_product_offline(p.get("id"))]

    state_counts = {"disabled": 0, "muted": 0}
    for m in merchants:
        st = platform_state.merchant_state(m.get("id"))
        if st["disabled"]:
            state_counts["disabled"] += 1
        if st["muted"]:
            state_counts["muted"] += 1

    total_amount = round(sum(_amount(o) for o in orders), 2)
    arbitrations = list_arbitrations()

    return {
        "merchant_count": len(merchants),
        "user_count": len(users),
        "product_count": len(products),
        "offline_count": len(offline),
        "order_count": len(orders),
        "order_amount": total_amount,
        "arbitration_count": len(arbitrations),
        "disabled_count": state_counts["disabled"],
        "muted_count": state_counts["muted"],
        "review_count": len(_review_rows()),
        "fine_total": round(sum(platform_state.fine_total(m.get("id")) for m in merchants), 2),
        "conversation_count": len(store.list_conversations()),
    }

def list_merchants():
    products = _all_products()
    sales = _merchant_sales(products)
    reviews = _review_rows()

    pids_by_owner = {}
    review_stat = {}
    for p in products:
        pids_by_owner.setdefault(str(p.get("owner_id") or ""), []).append(str(p.get("id")))
    for r in reviews:
        for owner, pids in pids_by_owner.items():
            if str(r.get("product_id")) in pids:
                item = review_stat.setdefault(owner, {"count": 0, "sum": 0})
                item["count"] += 1
                item["sum"] += int(r.get("rating") or 0)
                break

    result = []
    for m in _merchant_rows():
        mid = str(m.get("id"))
        st = platform_state.merchant_state(mid)
        own_products = [p for p in products if str(p.get("owner_id") or "") == mid]
        s = sales.get(mid) or {}
        rs = review_stat.get(mid) or {"count": 0, "sum": 0}
        result.append({
            "id": mid,
            "username": m.get("username") or "",
            "name": m.get("name") or "",
            "shop_name": m.get("shop_name") or m.get("name") or "",
            "create_time": _fmt(m.get("create_time")),
            "disabled": st["disabled"],
            "muted": st["muted"],
            "product_count": len(own_products),
            "offline_count": sum(
                1 for p in own_products if platform_state.is_product_offline(p.get("id"))
            ),
            "order_count": int(s.get("order_count") or 0),
            "sales": int(s.get("sales") or 0),
            "sales_amount": round(float(s.get("amount") or 0), 2),
            "buyer_count": len(s.get("buyers") or []),
            "fine_total": platform_state.fine_total(mid),
            "review_count": rs["count"],
            "review_avg": round(rs["sum"] / rs["count"], 1) if rs["count"] else 0,
            "needs_human": sum(
                1 for c in store.list_conversations(merchant_id=mid, include_platform=False)
                if c.get("needs_human")
            ),
        })
    result.sort(key=lambda x: x["sales_amount"], reverse=True)
    return result

def merchant_detail(mid):
    mid = str(mid or "").strip()
    merchant = next((m for m in list_merchants() if m["id"] == mid), None)
    if not merchant:
        return None

    products = [p for p in _all_products() if str(p.get("owner_id") or "") == mid]
    stats = _product_sales(products)
    product_list = []
    for p in products:
        st = platform_state.product_state(p.get("id"))
        s = stats.get(str(p.get("id"))) or {}
        product_list.append({
            "id": p.get("id"),
            "name": p.get("name") or "",
            "tag": p.get("tag") or "",
            "price": p.get("price"),
            "views": int(p.get("views") or 0),
            "sales": int(s.get("sales") or 0),
            "sales_amount": round(float(s.get("amount") or 0), 2),
            "buyer_count": len(s.get("buyers") or []),
            "offline": st["offline"],
            "offline_reason": st["reason"],
            "offline_time": st["time"],
        })
    product_list.sort(key=lambda x: x["sales_amount"], reverse=True)

    pids = {p["id"] for p in product_list}
    reviews = []
    for r in _review_rows():
        if str(r.get("product_id")) in pids:
            reviews.append({
                "id": r.get("id"),
                "product_id": r.get("product_id"),
                "product_name": next((p["name"] for p in product_list
                                      if p["id"] == str(r.get("product_id"))), ""),
                "user_name": r.get("user_name") or "",
                "sex": r.get("user_sex") or "未知",
                "rating": int(r.get("rating") or 0),
                "content": r.get("content") or "",
                "create_time": _fmt(r.get("create_time")),
            })

    orders = []
    for o in _all_orders():
        if str(o.get("merchant_id") or "") == mid:
            orders.append(o)
        elif not o.get("merchant_id") and str(o.get("product_id") or "") in pids:
            orders.append(o)
    order_list = [{
        "order_id": o.get("id"),
        "goodsname": o.get("goodsname") or "",
        "quantity": int(o.get("quantity") or 0),
        "amount": round(_amount(o), 2),
        "user_id": str(o.get("user_id") or ""),
        "user_name": o.get("name") or "",
        "address": o.get("address") or "",
        "waybill": o.get("waybill") or "",
        "create_time": _fmt(o.get("create_time")),
    } for o in orders]

    return {
        "merchant": merchant,
        "products": product_list,
        "orders": order_list,
        "reviews": reviews,
        "fines": platform_state.list_fines(mid),
        "conversations": store.list_conversations(merchant_id=mid, include_platform=False),
    }

def list_users():
    orders = _all_orders()
    stats = {}
    for o in orders:
        uid = str(o.get("user_id") or "")
        if not uid:
            continue
        item = stats.setdefault(uid, {"order_count": 0, "quantity": 0, "amount": 0.0})
        item["order_count"] += 1
        item["quantity"] += int(o.get("quantity") or 0)
        item["amount"] = round(item["amount"] + _amount(o), 2)

    by_uid = {}
    for o in orders:
        by_uid.setdefault(str(o.get("user_id") or ""), []).append(o)

    result = []
    for u in _user_rows():
        uid = str(u.get("id"))
        s = stats.get(uid) or {"order_count": 0, "quantity": 0, "amount": 0.0}
        convs = [c for c in store.list_conversations() if str(c.get("user_id")) == uid]
        result.append({
            "id": uid,
            "name": u.get("name") or "",
            "age": u.get("age"),
            "sex": u.get("sex") or "未知",
            "order_count": s["order_count"],
            "quantity": s["quantity"],
            "amount": round(float(s["amount"]), 2),
            "conversation_count": len(convs),
            "needs_human": sum(1 for c in convs if c.get("needs_human")),
        })
    result.sort(key=lambda x: x["amount"], reverse=True)
    return result

def user_detail(uid):
    uid = str(uid or "").strip()
    user = next((u for u in list_users() if u["id"] == uid), None)
    if not user:
        return None
    orders = get_orders_by_user(uid, limit=None) or []
    order_list = [{
        "order_id": o.get("id"),
        "product_id": o.get("product_id") or "",
        "goodsname": o.get("goodsname") or "",
        "quantity": int(o.get("quantity") or 0),
        "price": o.get("price"),
        "address": o.get("address") or "",
        "waybill": o.get("waybill") or "",
        "merchant_name": o.get("merchant_name") or "",
        "status_text": o.get("status_text") or "",
        "create_time": _fmt(o.get("create_time")),
    } for o in orders]
    return {
        "user": user,
        "orders": order_list,
        "returns": get_returns_by_user(uid) or [],
        "conversations": [c for c in store.list_conversations()
                          if str(c.get("user_id")) == uid],
    }

# 商品 / 评价
def list_products():
    products = _all_products()
    stats = _product_sales(products)
    result = []
    for p in products:
        st = platform_state.product_state(p.get("id"))
        s = stats.get(str(p.get("id"))) or {}
        result.append({
            "id": p.get("id"),
            "name": p.get("name") or "",
            "tag": p.get("tag") or "",
            "price": p.get("price"),
            "owner_id": str(p.get("owner_id") or ""),
            "owner_name": p.get("owner_name") or "",
            "views": int(p.get("views") or 0),
            "sales": int(s.get("sales") or 0),
            "sales_amount": round(float(s.get("amount") or 0), 2),
            "offline": st["offline"],
            "offline_reason": st["reason"],
            "offline_time": st["time"],
        })
    result.sort(key=lambda x: x["sales_amount"], reverse=True)
    return result

def list_all_reviews():
    products = {str(p.get("id")): p for p in _all_products()}
    merchant_names = {str(m.get("id")): (m.get("shop_name") or m.get("name") or "")
                      for m in _merchant_rows()}
    result = []
    for r in _review_rows():
        pid = str(r.get("product_id") or "")
        p = products.get(pid) or {}
        owner = str(p.get("owner_id") or "")
        result.append({
            "id": r.get("id"),
            "product_id": pid,
            "product_name": p.get("name") or "（商品已删除）",
            "merchant_id": owner,
            "merchant_name": merchant_names.get(owner) or p.get("owner_name") or "",
            "user_name": r.get("user_name") or "",
            "sex": r.get("user_sex") or "未知",
            "rating": int(r.get("rating") or 0),
            "content": r.get("content") or "",
            "create_time": _fmt(r.get("create_time")),
        })
    return result

# 会话（人机 / 人工）
def list_all_conversations():
    """全部会话，附带店铺名与「人机 / 人工」标记。"""
    merchant_names = {str(m.get("id")): (m.get("shop_name") or m.get("name") or "")
                      for m in _merchant_rows()}
    result = []
    for c in store.list_conversations():
        mid = str(c.get("merchant_id") or "platform")
        item = dict(c)
        item["merchant_name"] = merchant_names.get(mid) or c.get("merchant_name") or (
            "平台公共会话" if mid == "platform" else mid
        )
        item["is_platform"] = (mid == "platform")
        item["mode"] = "人工" if c.get("needs_human") or c.get("human_handled") else "人机"
        result.append(item)
    return result

def platform_thread(merchant_id, mark_read=True):
    """平台与某个商家的一对一会话（平台侧读）。"""
    mid = str(merchant_id or "").strip()
    if not mid:
        return None
    thread = store.get_thread(mid, PLATFORM_USER_ID)
    if mark_read:
        store.mark_read(mid, PLATFORM_USER_ID, "user")
    return thread

def platform_reply(merchant_id, text, merchant_name=""):
    """平台以「平台管理员」身份给商家发消息，商家在「顾客消息」里可见并回复。"""
    mid = str(merchant_id or "").strip()
    text = str(text or "").strip()
    if not mid or not text:
        return False, "缺少商家或消息内容", None
    message = store.append_message(
        mid, PLATFORM_USER_ID, PLATFORM_USER_NAME, "user", text,
        merchant_name=merchant_name,
    )
    return True, "已发送", message