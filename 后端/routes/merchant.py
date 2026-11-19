"""商户后台接口：账号注册 / 登录、商品管理、顾客会话。

与客户接口同源（都由 Flask 提供），数据通过 公共/store.py 与 公共/accounts.py
共享，因此「商家上架 → 客户可见」「顾客留言 → 商家回复」天然打通。

鉴权：登录后返回 token，前端放在 Authorization: Bearer <token>（或 X-Merchant-Token）。
商品与会话都按登录商户隔离，实现多个商户各管各的。
"""
from flask import Blueprint, jsonify, request

from 公共 import store
from 公共.accounts import (
    register_merchant,
    verify_merchant,
    get_merchant_by_token,
    logout_merchant,
)
from 售后.order import (
    product_order_stats, list_orders_for_owner, ship_orders,
    orders_belong_to_merchant,
)
from 售后.returns import list_returns_for_owner, reship_exchange, set_after_sale_status
from 物流.repository import get_logistics
from 物流.tracking_api import is_external_waybill
from 公共.knowledge import async_sync_knowledge
from 智能分析 import 用户画像 as profiles_mod
from 智能分析 import 购买预测 as predict_mod

bp = Blueprint("merchant_admin", __name__, url_prefix="/api/merchant")

def _token_from_request():
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Merchant-Token")
            or request.args.get("token")
            or (request.get_json(silent=True) or {}).get("token")
            or "")

def current_merchant():
    return get_merchant_by_token(_token_from_request())

def require_merchant():
    m = current_merchant()
    if not m:
        return None, (jsonify({"ok": False, "error": "请先登录商户账号", "code": "NO_AUTH"}), 401)
    return m, None

@bp.post("/register")
def api_merchant_register():
    data = request.get_json(silent=True) or {}
    ok, info, msg = register_merchant(
        username=data.get("username"),
        password=data.get("password"),
        password2=data.get("password2"),
        name=data.get("name"),
        shop_name=data.get("shop_name"),
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "merchant": info, "message": msg}), 201

@bp.post("/login")
def api_merchant_login():
    data = request.get_json(silent=True) or {}
    ok, info, msg = verify_merchant(data.get("username"), data.get("password"))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 401
    return jsonify({"ok": True, "merchant": info, "token": info.get("token")})

@bp.post("/logout")
def api_merchant_logout():
    logout_merchant(_token_from_request())
    return jsonify({"ok": True})

@bp.get("/me")
def api_merchant_me():
    m, err = require_merchant()
    if err:
        return err
    return jsonify({"ok": True, "merchant": m})

@bp.get("/products")
def api_merchant_products():
    m, err = require_merchant()
    if err:
        return err
    products = store.list_products(owner_id=m["id"], include_offline=True)
    from 公共.platform_state import product_state
    stats = product_order_stats(products)
    for p in products:
        s = stats.get(str(p.get("id"))) or {}
        st = product_state(p.get("id"))
        p["offline"] = st["offline"]
        p["offline_reason"] = st["reason"]
        p["views"] = int(p.get("views") or 0)
        p["sales"] = int(s.get("sales") or 0)
        p["sales_amount"] = round(float(s.get("sales_amount") or 0), 2)
        p["order_count"] = int(s.get("order_count") or 0)
        p["buyer_count"] = int(s.get("buyer_count") or 0)
    return jsonify({
        "ok": True,
        "products": products,
        "merchant": m,
    })

@bp.get("/products/<pid>/orders")
def api_merchant_product_orders(pid):
    """某个商品的订单明细 + 买家汇总（购买次数 / 订单号 / 物流）。"""
    m, err = require_merchant()
    if err:
        return err
    product = store.get_product(pid, include_offline=True)
    if not product or str(product.get("owner_id") or "") != str(m["id"]):
        return jsonify({"ok": False, "error": "商品不存在或无权查看"}), 404

    stats = product_order_stats([product]).get(str(pid)) or {}
    orders = stats.get("orders") or []

    buyers = {}
    for o in orders:
        key = str(o.get("user_id") or o.get("user_name") or "?")
        b = buyers.setdefault(key, {
            "user_id": o.get("user_id") or "",
            "user_name": o.get("user_name") or "",
            "count": 0, "quantity": 0, "amount": 0.0, "orders": [],
        })
        b["count"] += 1
        b["quantity"] += int(o.get("quantity") or 0)
        b["amount"] = round(b["amount"] + float(o.get("amount") or 0), 2)
        b["orders"].append({
            "order_id": o.get("order_id"),
            "waybill": o.get("waybill") or "",
            "status_text": o.get("status_text") or "",
            "quantity": o.get("quantity"),
            "create_time": o.get("create_time") or "",
        })
    buyer_list = sorted(buyers.values(), key=lambda x: x["count"], reverse=True)

    return jsonify({
        "ok": True,
        "product": {
            "id": product.get("id"),
            "name": product.get("name"),
            "tag": product.get("tag"),
            "price": product.get("price"),
            "views": int(product.get("views") or 0),
            "sales": int(stats.get("sales") or 0),
            "sales_amount": round(float(stats.get("sales_amount") or 0), 2),
            "order_count": int(stats.get("order_count") or 0),
            "buyer_count": int(stats.get("buyer_count") or 0),
        },
        "orders": orders,
        "buyers": buyer_list,
    })

@bp.post("/products")
def api_merchant_create_product():
    m, err = require_merchant()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    product = store.normalize_product(body)
    if not product["name"]:
        return jsonify({"ok": False, "error": "商品名称不能为空"}), 400
    product["owner_id"] = m["id"]
    product["owner_name"] = m.get("shop_name") or m.get("name") or ""
    product = store.create_product(product)
    # 新品在库存表初始化 SKU 行（默认库存），否则客户看到 0 库存无法下单
    from 公共 import inventory
    inventory.sync_skus(product)
    # 商品落盘到 商品咨询 文件夹 + 异步同步到 Dify 知识库（不阻塞返回）
    async_sync_knowledge("create", product)
    return jsonify({"ok": True, "product": product}), 201

@bp.put("/products/<pid>")
def api_merchant_update_product(pid):
    m, err = require_merchant()
    if err:
        return err
    existing = store.get_product(pid, include_offline=True)
    if not existing:
        return jsonify({"ok": False, "error": "商品不存在"}), 404
    if str(existing.get("owner_id") or "") != str(m["id"]):
        return jsonify({"ok": False, "error": "无权修改其他商户的商品"}), 403
    product = store.update_product(pid, request.get_json(silent=True) or {})
    # 同步 SKU 定义到库存表（新增规格自动建库存行，旧规格保持不变）
    from 公共 import inventory
    inventory.sync_skus(product)
    async_sync_knowledge("update", product)
    return jsonify({"ok": True, "product": product})

@bp.delete("/products/<pid>")
def api_merchant_delete_product(pid):
    m, err = require_merchant()
    if err:
        return err
    existing = store.get_product(pid, include_offline=True)
    if not existing:
        return jsonify({"ok": False, "error": "商品不存在"}), 404
    if str(existing.get("owner_id") or "") != str(m["id"]):
        return jsonify({"ok": False, "error": "无权删除其他商户的商品"}), 403
    store.delete_product(pid)
    async_sync_knowledge("delete", {"id": pid})
    return jsonify({"ok": True})

# 库存维护（商家：查看 / 设置 / 增减 SKU 库存）
@bp.get("/inventory")
def api_merchant_inventory():
    """本商户全部商品的 SKU 实时库存（stock / locked / sold / available）。"""
    m, err = require_merchant()
    if err:
        return err
    from 公共 import inventory
    products = store.list_products(owner_id=m["id"], include_offline=True)
    products = inventory.decorate_products(products)
    return jsonify({"ok": True, "products": products})

@bp.post("/inventory/<pid>")
def api_merchant_set_inventory(pid):
    """设置 / 增减某商品的 SKU 库存。

    请求体二选一：
      {"stocks": {"<sku_id>": <库存数>}}    —— 按绝对值设置（不能低于已预占）
      {"adjust": {"<sku_id>": <增减量>}}      —— 正数补货、负数扣减（盘盈盘亏）
    返回更新后的商品（含各 SKU 实时库存）。
    """
    m, err = require_merchant()
    if err:
        return err
    product = store.get_product(pid, include_offline=True)
    if not product or str(product.get("owner_id") or "") != str(m["id"]):
        return jsonify({"ok": False, "error": "商品不存在或无权操作"}), 404
    body = request.get_json(silent=True) or {}
    from 公共 import inventory

    # 增减模式：逐 SKU 调 adjust_stock，互不影响
    if "adjust" in body and isinstance(body["adjust"], dict):
        results = []
        for sid, delta in body["adjust"].items():
            ok, msg = inventory.adjust_stock(sid, delta)
            results.append({"sku_id": str(sid), "ok": ok, "msg": msg})
        ok_all = all(r["ok"] for r in results)
        updated = inventory.decorate_products([product])[0]
        return jsonify({"ok": ok_all, "results": results, "product": updated}), \
               (200 if ok_all else 400)

    # 绝对值模式：按目标库存设置（受已预占约束）
    stocks = body.get("stocks")
    if not isinstance(stocks, dict):
        return jsonify({
            "ok": False,
            "error": "请传入 stocks（{sku_id: 库存数}）或 adjust（{sku_id: 增减量}）",
        }), 400
    try:
        targets = {str(k): int(v) for k, v in stocks.items()}
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "库存数必须是非负整数"}), 400
    ok, msg, _ = inventory.sync_skus(product, stocks=targets)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    updated = inventory.decorate_products([product])[0]
    return jsonify({"ok": True, "message": msg, "product": updated})

@bp.post("/upload")
def api_merchant_upload():
    m, err = require_merchant()
    if err:
        return err
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    stored = store.save_image(file.filename, file.read())
    return jsonify({"ok": True, "filename": stored, "url": "/images/" + stored})

# 订单管理（商家：查看全部订单 / 随时发货）
def _owner_products(m):
    """当前商户的商品，返回 (product_ids, product_names)。"""
    products = store.list_products(owner_id=m["id"])
    return ([str(p.get("id")) for p in products if p.get("id")],
            [p.get("name") for p in products if p.get("name")])

@bp.get("/orders")
def api_merchant_orders():
    """商户的全部订单（可按顾客 / 发货状态过滤）。

    查询参数：user_id（只看向某个顾客的订单）、status=pending|shipped。
    """
    m, err = require_merchant()
    if err:
        return err
    product_ids, product_names = _owner_products(m)
    user_id = str(request.args.get("user_id") or "").strip()
    status = str(request.args.get("status") or "").strip().lower()
    if status not in ("pending", "shipped"):
        status = None
    orders = list_orders_for_owner(
        product_ids, product_names, user_id=user_id or None, status=status,
        merchant_id=m["id"],
    )
    return jsonify({
        "ok": True,
        "orders": orders,
        "stats": {
            "total": len(orders),
            # 待发货红点：只数「还需要发货」的订单。已申请退货/退款完成等
            "pending": sum(1 for o in orders
                           if int(o.get("status_step") or 0) == 0 and o.get("can_ship", True)),
            "shipped": sum(1 for o in orders if int(o.get("status_step") or 0) > 0),
        },
    })

@bp.post("/orders/<order_id>/ship")
def api_merchant_ship_order(order_id):
    """发货：填写 7 位快递单号，开始计算送达时间（默认 5 分钟）。"""
    m, err = require_merchant()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    product_ids, product_names = _owner_products(m)
    ok, msg, logistics = ship_orders(
        order_id, body.get("waybill"), product_ids, product_names,
        merchant_id=m["id"],
    )
    return jsonify({"ok": ok, "message": msg, "logistics": logistics}), (200 if ok else 400)

# 物流查询（商家）
@bp.get("/logistics")
def api_merchant_logistics():
    m, err = require_merchant()
    if err:
        return err
    waybill = str(request.args.get("waybill") or "").strip()
    if not waybill:
        return jsonify({"ok": False, "error": "请输入运单号"}), 400
    # 非 7 位 = 外部真实快递单号，按约定仅平台管理员可查
    if is_external_waybill(waybill):
        return jsonify({
            "ok": False,
            "error": "本店运单请使用 7 位运单号；外部真实快递单号请到平台管理页「快递查询」中查询",
        }), 403
    found, row = get_logistics(waybill)
    if not found:
        return jsonify({"ok": False, "error": "未查询到该运单号"}), 404

    # 物权校验：仅当该物流关联的订单里至少一单属于当前商户，才允许查询
    product_ids, product_names = _owner_products(m)
    if not _logistics_belongs_to_merchant(row, product_ids, product_names, m["id"]):
        return jsonify({"ok": False, "error": "无权查看非本店铺的物流信息"}), 403

    return jsonify({"ok": True, "logistics": row})

def _logistics_belongs_to_merchant(row, product_ids, product_names, merchant_id=None):
    """该物流是否至少关联一笔属于当前商户的订单（按订单ID精确核对）。"""
    raw = (row or {}).get("order_ids")
    if isinstance(raw, str):
        order_ids = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        order_ids = [str(x) for x in (raw or []) if str(x).strip()]
    if not order_ids:
        return False
    owned = orders_belong_to_merchant(
        order_ids, merchant_id, product_ids, product_names
    )
    return len(owned) > 0

# 售后处理（退货 / 换货 / 退款，商家）
def _merchant_product_names(m):
    return [p.get("name") for p in store.list_products(owner_id=m["id"])]

@bp.get("/refunds")
def api_merchant_refunds():
    m, err = require_merchant()
    if err:
        return err
    # 顾客通过售后客服或订单页提交的退货 / 换货 / 退款全部要展示给商家确认
    refunds = list_returns_for_owner(m["id"], _merchant_product_names(m))
    return jsonify({"ok": True, "refunds": refunds})

@bp.post("/refunds/<rid>/status")
def api_merchant_refund_status(rid):
    m, err = require_merchant()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    status = str(body.get("status") or "next").strip()
    # 拒绝时必须填写理由（说明为什么不通过），该理由会展示给顾客
    ok, msg, data = set_after_sale_status(
        rid, status, owner_id=m["id"], product_names=_merchant_product_names(m),
        reject_reason=body.get("reject_reason"),
    )
    return jsonify({"ok": ok, "message": msg, "refund": data}), (200 if ok else 400)

@bp.post("/refunds/<rid>/reship")
def api_merchant_refund_reship(rid):
    """换货：商家签收顾客寄回的商品后，填写新的 7 位运单号重新发货。"""
    m, err = require_merchant()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ok, msg, data = reship_exchange(
        rid, body.get("waybill"),
        owner_id=m["id"], product_names=_merchant_product_names(m),
    )
    return jsonify({"ok": ok, "message": msg, "refund": data}), (200 if ok else 400)

# 顾客会话
@bp.get("/chats")
def api_merchant_chats():
    m, err = require_merchant()
    if err:
        return err
    return jsonify({
        "ok": True,
        "conversations": store.list_conversations(merchant_id=m["id"], include_platform=False),
        # 售后转人工的会话登记在平台线程（merchant_id=platform），
        # 商家这边也要能一眼看到「有顾客在等人工」，所以单独给个数量。
        # 只统计「还没人接手」的（平台客服点开即算接手），否则商家侧会一直闪个不停。
        "platform_human": sum(1 for c in store.list_conversations(merchant_id="platform")
                              if c.get("waiting_human")),
    })

@bp.get("/chats/search")
def api_merchant_chats_search():
    """聊天记录查询：按关键词（顾客名 / 消息内容）与时间范围搜索本店会话。"""
    m, err = require_merchant()
    if err:
        return err
    args = request.args
    return jsonify({
        "ok": True,
        "results": store.search_threads(
            merchant_id=m["id"],
            keyword=args.get("q") or "",
            since=args.get("since") or "",
            until=args.get("until") or "",
        ),
    })

@bp.get("/chats/thread")
def api_merchant_thread():
    m, err = require_merchant()
    if err:
        return err
    user_id = str(request.args.get("user_id") or "").strip()
    merchant_id = m["id"]
    if not user_id:
        return jsonify({"ok": False, "error": "缺少 user_id"}), 400
    # 先标已读再取会话：返回的消息里已读标记就是最新的（顾客那条会显示「已读」）
    store.mark_read(merchant_id, user_id, "merchant")
    thread = store.get_thread(merchant_id, user_id)
    return jsonify({"ok": True, "thread": thread})

@bp.post("/chats/clear-intent")
def api_merchant_clear_intent():
    """商家在聊天里「标记已处理」退换货意向：清除该会话的退换货提醒标记。"""
    m, err = require_merchant()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    user_id = str(data.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "缺少 user_id"}), 400
    store.clear_return_intent(m["id"], user_id)
    return jsonify({"ok": True, "message": "已标记处理完成"})

@bp.post("/chats/reply")
def api_merchant_reply():
    m, err = require_merchant()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    user_id = str(data.get("user_id") or "").strip()
    # 与「平台管理员」的会话不受禁言影响：禁言期间商家只能和平台管理员对话
    to_platform = (user_id == "platform")
    if m.get("disabled") and not to_platform:
        return jsonify({"ok": False, "error": "该商家账号已被平台停用，无法回复顾客"}), 403
    if m.get("muted") and not to_platform:
        return jsonify({"ok": False, "error": "该商家账号已被平台禁言，暂时无法回复顾客（仅可与平台管理员对话）"}), 403
    # 同上：只能回复自己店铺的会话，不接受请求里传来的 merchant_id
    merchant_id = m["id"]
    text = str(data.get("text") or "").strip()
    if not user_id or not text:
        return jsonify({"ok": False, "error": "缺少 user_id 或消息内容"}), 400
    message = store.append_message(
        merchant_id, user_id, data.get("user_name"), "merchant", text,
        merchant_name=m.get("shop_name") or m.get("name") or "",
    )
    return jsonify({"ok": True, "message": message})

# 知识库同步（异步后台同步到 Dify 知识库）
@bp.post("/knowledge/update")
def api_merchant_knowledge():
    """商品新增 / 修改 / 删除时同步到 Dify 知识库。

    同步在后台线程执行，请求立即返回，因此「同步」与「商品查找 / 管理」并行不阻塞；
    本地记录 product_id→document_id 映射，供后续更新 / 删除定位对应文档。
    请求体示例：{"action": "create|update|delete", "product": {...}}
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    product = body.get("product") or {}
    result = async_sync_knowledge(action, product)
    return jsonify(result)

# 智能分析（顾客洞察 / 商品购买可能性）
@bp.get("/analytics/customers")
def api_merchant_analytics_customers():
    """本店相关顾客画像：在本店下过单、或浏览 / 加购过本店商品的顾客。"""
    m, err = require_merchant()
    if err:
        return err
    return jsonify({"ok": True, "profiles": profiles_mod.profiles_by_merchant(m["id"])})

@bp.get("/analytics/purchase")
def api_merchant_analytics_purchase():
    """本店商品的购买可能性：潜在买家排名 + 全部「顾客×商品」概率前 20。"""
    m, err = require_merchant()
    if err:
        return err
    return jsonify({
        "ok": True,
        "products": predict_mod.product_prospects(mid=m["id"]),
        "top": predict_mod.top_prospects(mid=m["id"], k=20),
    })
