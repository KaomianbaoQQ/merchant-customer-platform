"""商品接口（客户前端）：读取商家后台维护的商品数据。

商家通过「商户界面/后端服务 server.py」上架任意商品，数据写入
公共/store.py 管理的 products.json，本接口读取同一份数据，
因此商家改动后客户前端刷新即可看到最新商品。
"""
from flask import Blueprint, jsonify, request

from 公共 import store
from 公共 import behavior

bp = Blueprint("products", __name__, url_prefix="/api")


@bp.get("/products")
def api_products():
    products = store.list_products()
    tag = str(request.args.get("tag") or "").strip()
    if tag:
        products = [p for p in products if str(p.get("tag") or "") == tag]
    return jsonify({
        "ok": True,
        "products": products,
        "categories": store.list_categories(),
    })


@bp.get("/products/<pid>")
def api_product(pid):
    product = store.get_product(pid)
    if not product:
        return jsonify({"ok": False, "error": "商品不存在"}), 404
    return jsonify({"ok": True, "product": product})


@bp.post("/products/<pid>/view")
def api_product_view(pid):
    """客户前端打开商品详情时调用，浏览量 +1；带登录身份时同时记一条浏览行为。"""
    views = store.increment_views(pid)
    if views is None:
        return jsonify({"ok": False, "error": "商品不存在"}), 404
    data = request.get_json(silent=True) or {}
    user = data.get("user") or {}
    uid = str(user.get("id") or "").strip()
    if uid:
        product = store.get_product(pid, include_offline=True) or {}
        behavior.record(
            uid, "view", product_id=pid,
            product_name=product.get("name") or "",
            merchant_id=str(product.get("owner_id") or ""),
            user_name=str(user.get("name") or ""),
        )
    return jsonify({"ok": True, "views": views})
