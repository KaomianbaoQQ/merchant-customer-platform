"""服务端购物车：购物车存数据库（按用户），登录后前端走这里，未登录回退 localStorage。

接口（均需在请求里带用户身份：Bearer 顾客令牌，或旧的 id+name）：
  GET    /api/cart                  列出当前用户购物车
  POST   /api/cart                  加入购物车（同商品重复加购数量累加）：{product_id, sku_id?, qty}
  PUT    /api/cart/<product_id>     改数量 / 选中：{qty?, selected?}
  DELETE /api/cart/<product_id>     删除某商品（sku_id 可选，区分同商品不同规格）
  DELETE /api/cart                  清空
"""
from flask import Blueprint, jsonify, request

from 公共.db_config import get_conn
from ..common import resolve_user

bp = Blueprint("cart", __name__, url_prefix="/api")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS `cart` (
    `id`         BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`    VARCHAR(64)  NOT NULL,
    `product_id` VARCHAR(128) NOT NULL,
    `sku_id`     VARCHAR(128) DEFAULT ''    COMMENT '规格ID，空表示默认规格',
    `qty`        INT          NOT NULL DEFAULT 1,
    `selected`   TINYINT      NOT NULL DEFAULT 1 COMMENT '是否选中（结算用）',
    `create_time` DATETIME    DEFAULT CURRENT_TIMESTAMP,
    `update_time` DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_user_sku` (`user_id`, `product_id`, `sku_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='购物车';
"""


def _ensure():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


def _require_user():
    user = resolve_user((request.get_json(silent=True) or {}).get("user"))
    if user is None:
        user = resolve_user({
            "id": request.args.get("user_id"),
            "name": request.args.get("user_name"),
        })
    return user


def _rows_to_list(rows):
    return [{
        "product_id": r["product_id"],
        "sku_id": r.get("sku_id") or "",
        "qty": int(r.get("qty") or 1),
        "selected": bool(r.get("selected")),
    } for r in rows]


@bp.get("/cart")
def api_cart_list():
    user = _require_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    _ensure()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, sku_id, qty, selected FROM `cart` "
                "WHERE user_id=%s ORDER BY create_time ASC", (str(user["id"]),)
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"ok": True, "items": _rows_to_list(rows)})


@bp.post("/cart")
def api_cart_add():
    user = _require_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    data = request.get_json(silent=True) or {}
    pid = str(data.get("product_id") or "").strip()
    sku = str(data.get("sku_id") or "").strip()
    try:
        qty = int(data.get("qty") or 1)
    except (TypeError, ValueError):
        qty = 1
    if qty < 0:
        qty = 0
    if not pid:
        return jsonify({"ok": False, "error": "缺少 product_id"}), 400

    _ensure()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if qty == 0:
                cur.execute(
                    "DELETE FROM `cart` WHERE user_id=%s AND product_id=%s AND sku_id=%s",
                    (str(user["id"]), pid, sku),
                )
            else:
                cur.execute(
                    "INSERT INTO `cart` (user_id, product_id, sku_id, qty, selected) "
                    "VALUES (%s, %s, %s, %s, 1) "
                    # 重复加购要「累加」而不是覆盖：点两次「加入购物袋」= 2 件
                    "ON DUPLICATE KEY UPDATE qty=LEAST(qty+VALUES(qty), 999), selected=1, "
                    "update_time=CURRENT_TIMESTAMP",
                    (str(user["id"]), pid, sku, qty),
                )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, sku_id, qty, selected FROM `cart` WHERE user_id=%s",
                (str(user["id"]),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"ok": True, "items": _rows_to_list(rows)})


@bp.put("/cart/<product_id>")
def api_cart_update(product_id):
    user = _require_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku_id") or "").strip()
    pid = str(product_id).strip()
    qty = data.get("qty")
    selected = data.get("selected")

    _ensure()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sets, params = [], []
            if qty is not None:
                try:
                    q = int(qty)
                except (TypeError, ValueError):
                    q = 1
                if q <= 0:
                    cur.execute(
                        "DELETE FROM `cart` WHERE user_id=%s AND product_id=%s AND sku_id=%s",
                        (str(user["id"]), pid, sku),
                    )
                    conn.commit()
                    return jsonify({"ok": True, "items": []})
                sets.append("qty=%s"); params.append(q)
            if selected is not None:
                sets.append("selected=%s"); params.append(1 if selected else 0)
            if not sets:
                return jsonify({"ok": False, "error": "无更新字段"}), 400
            params += [str(user["id"]), pid, sku]
            cur.execute(
                "UPDATE `cart` SET " + ", ".join(sets) + " WHERE user_id=%s AND product_id=%s AND sku_id=%s",
                params,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, sku_id, qty, selected FROM `cart` WHERE user_id=%s",
                (str(user["id"]),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"ok": True, "items": _rows_to_list(rows)})


@bp.delete("/cart/<product_id>")
def api_cart_remove(product_id):
    user = _require_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    sku = str((request.get_json(silent=True) or {}).get("sku_id") or request.args.get("sku_id") or "").strip()
    _ensure()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM `cart` WHERE user_id=%s AND product_id=%s AND sku_id=%s",
                (str(user["id"]), str(product_id).strip(), sku),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, sku_id, qty, selected FROM `cart` WHERE user_id=%s",
                (str(user["id"]),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"ok": True, "items": _rows_to_list(rows)})


@bp.delete("/cart")
def api_cart_clear():
    user = _require_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    _ensure()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM `cart` WHERE user_id=%s", (str(user["id"]),))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "items": []})
