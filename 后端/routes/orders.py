"""下单结算接口：写 cs.order（含收货地址）。

运单号不再在下单时自动生成：订单先进入「待发货」，
商家在商户后台填写 7 位快递单号后才写入物流并开始计算送达时间。
"""
import json

from flask import Blueprint, jsonify, request, current_app

from 公共.regions import list_provinces, list_cities, list_districts, validate_region, load_regions
from 公共 import store
from 公共 import behavior
from 售后.order import create_checkout_orders, get_orders_by_user
from 售后.returns import get_returns_by_user
from ..common import resolve_user

bp = Blueprint("orders", __name__, url_prefix="/api")

@bp.get("/regions")
def api_regions():
    """全国行政区划（省 / 市 / 区），供下单地址级联选择。

    不带参数：返回全量嵌套 {省: {市: [区, ...]}}，前端一次拉取后本地级联；
    带 province / city：返回对应市 / 区列表（调试用）。
    """
    data = request.args
    province = str(data.get("province") or "").strip()
    city = str(data.get("city") or "").strip()
    if province and city:
        return jsonify({"ok": True, "districts": list_districts(province, city)})
    if province:
        return jsonify({"ok": True, "cities": list_cities(province)})
    # 不排序，保持行政区划数据原有顺序
    body = json.dumps({"ok": True, "provinces": load_regions()}, ensure_ascii=False)
    return current_app.response_class(body, mimetype="application/json")

@bp.get("/orders")
def api_my_orders():
    """当前用户的全部订单（含物流状态）与退换货记录。"""
    user = resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    return jsonify({
        "ok": True,
        "orders": get_orders_by_user(user["id"]),
        "returns": get_returns_by_user(user["id"]),
    })

@bp.post("/orders")
def api_orders():
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再结算"}), 401
    if user.get("is_admin"):
        return jsonify({"ok": False, "error": "管理员账号用于后台管理，不能在商城前台下单"}), 403

    # 入库地址 = 省市区（校验后拼接）+ 详细地址
    province = str(data.get("province") or "").strip()
    city = str(data.get("city") or "").strip()
    district = str(data.get("district") or "").strip()
    detail = str(data.get("detail") or "").strip()
    ok_region, region_msg = validate_region(province, city, district)
    if not ok_region:
        return jsonify({"ok": False, "error": region_msg}), 400
    if not detail:
        return jsonify({"ok": False, "error": "请填写详细地址（街道、门牌号等）"}), 400
    address = f"{province}{city}{district}{detail}"
    receiver_name = str(data.get("receiver_name") or "").strip()
    phone = str(data.get("phone") or "").strip()
    if not receiver_name:
        return jsonify({"ok": False, "error": "请填写收货人姓名"}), 400
    if not phone:
        return jsonify({"ok": False, "error": "请填写联系电话"}), 400
    import re as _re
    if not _re.match(r"^[0-9\-+\s]{6,20}$", phone):
        return jsonify({"ok": False, "error": "联系电话格式不正确"}), 400

    items = data.get("items") or []
    items = [it for it in items if isinstance(it, dict) and (it.get("name") or "").strip()]
    if not items:
        return jsonify({"ok": False, "error": "购物袋为空"}), 400

    # 平台已下架的商品不能下单（顾客可能还停在旧页面 / 旧购物袋里）
    from 公共.platform_state import offline_product_ids
    offline = offline_product_ids()
    blocked = [str(it.get("product_id") or "") for it in items
               if str(it.get("product_id") or "") in offline]
    if blocked:
        names = []
        for it in items:
            if str(it.get("product_id") or "") in offline:
                names.append(str(it.get("name") or it.get("product_id")))
        return jsonify({
            "ok": False,
            "error": "「%s」已被平台下架，请刷新页面后从购物袋中移除" % "、".join(names),
            "offline": blocked,
        }), 409

    ok, msg, order_ids = create_checkout_orders(
        items, user, address, None, phone=phone, receiver_name=receiver_name
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    # 行为埋点：下单成功后逐个商品记一条 purchase，供画像与购买预测使用
    for it in items:
        pid = str(it.get("product_id") or "").strip()
        product = store.get_product(pid, include_offline=True) if pid else None
        behavior.record(
            user["id"], "purchase",
            product_id=pid,
            product_name=(product or {}).get("name") or it.get("name") or "",
            merchant_id=str((product or {}).get("owner_id") or ""),
            user_name=user.get("name") or "",
            extra={"quantity": it.get("qty")},
        )

    return jsonify({
        "ok": True,
        "message": msg,
        "order_ids": order_ids,
    })

@bp.post("/orders/<order_id>/confirm")
def api_confirm_receipt(order_id):
    """用户确认收货：订单置为「已完成」。仅订单归属人可操作。"""
    user = resolve_user((request.get_json(silent=True) or {}).get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    from 售后.order import confirm_receipt
    ok, msg, status = confirm_receipt(order_id, user["id"])
    return jsonify({"ok": ok, "message": msg, "order_status": status}), (200 if ok else 400)

@bp.post("/orders/<order_id>/pay")
def api_pay_order(order_id):
    """模拟支付成功：标记已支付并确认库存预占（不接真网关）。

    一次结算可能拆成多笔订单（不同 SKU / 不同商家），它们共享同一个
    reservation_id，支付任意一笔即整组生效。
    """
    user = resolve_user((request.get_json(silent=True) or {}).get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    from 售后.order import pay_by_order
    ok, msg = pay_by_order(order_id, user["id"])
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

@bp.post("/orders/<order_id>/cancel")
def api_cancel_order(order_id):
    """发货前取消订单：订单置为「已取消」。仅订单归属人可操作。"""
    user = resolve_user((request.get_json(silent=True) or {}).get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    from 售后.order import cancel_order
    ok, msg, status = cancel_order(order_id, user["id"])
    return jsonify({"ok": ok, "message": msg, "order_status": status}), (200 if ok else 400)
