"""商品购买可能性预测：给「顾客 × 商品」打一个 0~1 的购买概率，并说明理由。

为什么不用 LSTM：本项目订单量极小（个位数），序列模型无法有效训练（只会过拟合）。
这里用**可解释的加性评分模型**（逻辑回归形式，权重按业务先验设定）：
    概率 = sigmoid(偏置 + Σ 权重·特征)
特征全部来自真实埋点与业务数据，每个特征都会转成一句中文「理由」，
所以管理员 / 商家看到的不是黑盒数字，而是「为什么可能买」。

特征（都归一化到 0~1）：
    复购      该顾客以前买过这件商品
    浏览      该顾客打开过这件商品详情（埋点 view）
    加购      该顾客把这件商品加入过购物袋（埋点 cart）
    类目匹配  该顾客历史购买中与该商品同 tag 的比例
    商品热度  该商品的全站浏览量 + 销量
    商品口碑  该商品平均评分
    顾客活跃  该顾客的订单数

对外：
    predict_for_user(uid)            某顾客对全部商品（或本店商品）的购买概率排序
    product_prospects(mid)           每个商品的潜在买家排名（商家/管理员用）
    top_prospects(mid, k)            全部「顾客×商品」按概率排序的前 k 条

只依赖标准库 + 项目内模块，无需额外依赖。
"""
import math

from 智能分析 import 用户画像 as profile_mod

# 权重（业务可调）：越大代表该信号对「会买」的贡献越强
WEIGHTS = {
    "repeat": 1.3,      # 复购过
    "cart": 1.2,        # 加过购物袋
    "view": 0.7,        # 浏览过
    "tag_match": 1.0,   # 类目偏好匹配
    "popularity": 0.5,  # 商品热度
    "rating": 0.4,      # 商品口碑
    "activity": 0.3,    # 顾客活跃度
}
BIAS = -2.4             # 偏置：没有任何信号时概率很低

_MIN_PROB = 0.02        # 展示下限，低于它的不列出


def _pct(value):
    """0~1 保留一位小数的百分比字符串。"""
    return f"{round(value * 100, 1)}%"


def _sales_map(products, orders):
    """{product_id: 销量件数}。"""
    out = {}
    for o in orders:
        pid = str(o.get("product_id") or "")
        if not pid:
            continue
        out[pid] = out.get(pid, 0) + int(o.get("quantity") or 0)
    return out


def _context(mid=None):
    """准备打分所需的上下文：画像上下文 + 商品热度 / 销量 / 评分。

    mid 不为空时只保留该商家的商品（商户「顾客洞察」用）。
    """
    ctx = profile_mod._load_ctx()
    products = ctx["products"]
    if mid:
        mid = str(mid)
        products = [p for p in products if str(p.get("owner_id") or "") == mid]

    all_orders = []
    for lst in ctx["orders_by_user"].values():
        all_orders.extend(lst)
    sales = _sales_map(products, all_orders)

    max_hot = 1
    for p in products:
        hot = int(p.get("views") or 0) + sales.get(str(p.get("id")), 0) * 3
        max_hot = max(max_hot, hot)

    hot_by_pid = {}
    for p in products:
        pid = str(p.get("id"))
        hot = int(p.get("views") or 0) + sales.get(pid, 0) * 3
        ratings = ctx["reviews_by_product"].get(pid) or []
        hot_by_pid[pid] = {
            "hot": hot / max_hot,                                  # 0~1 热度
            "rating": (sum(ratings) / len(ratings) / 5) if ratings else 0.0,  # 0~1 口碑
            "sales": sales.get(pid, 0),
        }
    ctx["products"] = products
    ctx["hot"] = hot_by_pid
    return ctx


def _tag_match(user_tags, product):
    """用户历史购买类目与该商品类目的匹配度（0~1）。"""
    tag = (product.get("tag") or "未分类")
    total = sum(n for _, n in user_tags) or 0
    if not total:
        return 0.0, tag
    got = sum(n for t, n in user_tags if t == tag)
    return round(got / total, 2), tag


def score_pair(profile, product, beh, ctx):
    """给一个「顾客 × 商品」打分。返回 (概率, 理由列表)。"""
    pid = str(product.get("id"))
    hot = ctx["hot"].get(pid) or {"hot": 0.0, "rating": 0.0, "sales": 0}
    bought = any(
        pid == str(o.get("product_id") or "")
        or pid == str((profile_mod._match_product(o, ctx) or {}).get("id") or "")
        for o in _user_orders(profile, ctx)
    )

    views = int(beh.get("view") or 0)
    carts = int(beh.get("cart") or 0)
    tag_match, tag = _tag_match(profile["top_tags_as_pairs"], product)
    activity = min(1.0, profile["order_count"] / 5)

    feats = {
        "repeat": 1.0 if bought else 0.0,
        "cart": min(1.0, carts / 2),
        "view": min(1.0, views / 5),
        "tag_match": tag_match,
        "popularity": hot["hot"],
        "rating": hot["rating"],
        "activity": activity,
    }
    z = BIAS + sum(WEIGHTS[k] * v for k, v in feats.items())
    prob = 1 / (1 + math.exp(-z))

    reasons = []
    if feats["repeat"]:
        reasons.append("买过这件商品（复购）")
    if carts:
        reasons.append(f"加购 {carts} 次")
    if views:
        reasons.append(f"浏览 {views} 次")
    if tag_match >= 0.5 and profile["order_count"]:
        reasons.append(f"偏好「{tag}」类目")
    if hot["hot"] >= 0.6:
        reasons.append("全站热销")
    if hot["rating"] >= 0.8:
        reasons.append("评价很好")
    if activity >= 0.8:
        reasons.append("活跃顾客")
    return prob, reasons


def _user_orders(profile, ctx):
    return ctx["orders_by_user"].get(str(profile["id"]), [])


def _prepare_profiles(ctx):
    """用同一份 ctx 生成画像，并补上内部用的 (tag, qty) 元组。"""
    rows = [profile_mod._profile_of(u, ctx) for u in profile_mod._users()]
    for p in rows:
        p["top_tags_as_pairs"] = [(t["tag"], t["qty"]) for t in p["top_tags"]]
    return rows, {p["id"]: p for p in rows}


# ------------------------------------------------------------
# 对外
# ------------------------------------------------------------
def predict_for_user(uid, mid=None, top=None, min_prob=_MIN_PROB):
    """某顾客对各商品的购买概率（倒序）。mid 限定只算该商家的商品。"""
    uid = str(uid or "").strip()
    ctx = _context(mid)
    profiles, by_uid = _prepare_profiles(ctx)
    profile = by_uid.get(uid)
    if not profile:
        return None
    beh_all = ctx["beh_matrix"]
    results = []
    for p in ctx["products"]:
        beh = beh_all.get((uid, str(p.get("id")))) or {}
        prob, reasons = score_pair(profile, p, beh, ctx)
        if prob < min_prob:
            continue
        results.append({
            "user_id": uid,
            "user_name": profile["name"],
            "product_id": p.get("id"),
            "product_name": p.get("name") or "",
            "tag": p.get("tag") or "",
            "price": p.get("price"),
            "owner_id": str(p.get("owner_id") or ""),
            "owner_name": p.get("owner_name") or "",
            "prob": round(prob, 3),
            "prob_text": _pct(prob),
            "reasons": reasons,
        })
    results.sort(key=lambda x: x["prob"], reverse=True)
    return results[:top] if top else results


def top_prospects(mid=None, k=30, min_prob=_MIN_PROB):
    """全部「顾客 × 商品」按购买概率排序的前 k 条。"""
    ctx = _context(mid)
    profiles, _ = _prepare_profiles(ctx)
    beh_all = ctx["beh_matrix"]
    out = []
    for profile in profiles:
        uid = profile["id"]
        for p in ctx["products"]:
            beh = beh_all.get((uid, str(p.get("id")))) or {}
            prob, reasons = score_pair(profile, p, beh, ctx)
            if prob < min_prob:
                continue
            out.append({
                "user_id": uid,
                "user_name": profile["name"],
                "level": profile["level"],
                "product_id": p.get("id"),
                "product_name": p.get("name") or "",
                "tag": p.get("tag") or "",
                "price": p.get("price"),
                "owner_id": str(p.get("owner_id") or ""),
                "owner_name": p.get("owner_name") or "",
                "prob": round(prob, 3),
                "prob_text": _pct(prob),
                "reasons": reasons,
            })
    out.sort(key=lambda x: x["prob"], reverse=True)
    return out[:k]


def product_prospects(mid=None, top_per_product=5, min_prob=_MIN_PROB):
    """按商品汇总潜在买家：每个商品列出「谁最可能买」。"""
    ctx = _context(mid)
    profiles, _ = _prepare_profiles(ctx)
    beh_all = ctx["beh_matrix"]

    by_product = {}
    for p in ctx["products"]:
        pid = str(p.get("id"))
        buyers = []
        for profile in profiles:
            beh = beh_all.get((profile["id"], pid)) or {}
            prob, reasons = score_pair(profile, p, beh, ctx)
            if prob < min_prob:
                continue
            buyers.append({
                "user_id": profile["id"],
                "user_name": profile["name"],
                "level": profile["level"],
                "prob": round(prob, 3),
                "prob_text": _pct(prob),
                "reasons": reasons,
            })
        buyers.sort(key=lambda x: x["prob"], reverse=True)
        hot = ctx["hot"].get(pid) or {}
        by_product[pid] = {
            "product_id": pid,
            "product_name": p.get("name") or "",
            "tag": p.get("tag") or "",
            "price": p.get("price"),
            "owner_id": str(p.get("owner_id") or ""),
            "owner_name": p.get("owner_name") or "",
            "views": int(p.get("views") or 0),
            "sales": hot.get("sales", 0),
            "prospect_count": len(buyers),
            "avg_prob": round(sum(b["prob"] for b in buyers) / len(buyers), 3) if buyers else 0,
            "top_buyers": buyers[:top_per_product],
        }
    rows = sorted(by_product.values(), key=lambda x: x["avg_prob"], reverse=True)
    return rows