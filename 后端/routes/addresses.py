"""地址簿接口（需登录）：收货地址的保存 / 复用 / 管理。

顾客在下单弹窗里把地址存进地址簿后，下次下单可直接选用；支持增删改与默认地址。
"""
from flask import Blueprint, jsonify, request

from 售后.address import (
    create_address,
    delete_address,
    list_addresses,
    set_default_address,
    update_address,
)
from ..common import resolve_user

bp = Blueprint("addresses", __name__, url_prefix="/api")


def _current_user(data=None):
    """POST / PUT / DELETE 从 body 取用户，GET 从 query 取用户。"""
    if data is not None:
        return resolve_user(data.get("user"))
    return resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })


@bp.get("/addresses")
def api_list_addresses():
    user = _current_user()
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    return jsonify({"ok": True, "addresses": list_addresses(user["id"])})


@bp.post("/addresses")
def api_create_address():
    data = request.get_json(silent=True) or {}
    user = _current_user(data)
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再保存地址"}), 401
    ok, msg, row = create_address(user["id"], data)
    return jsonify({"ok": ok, "message": msg, "address": row}), (200 if ok else 400)


@bp.put("/addresses/<int:addr_id>")
def api_update_address(addr_id):
    data = request.get_json(silent=True) or {}
    user = _current_user(data)
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再修改地址"}), 401
    ok, msg, row = update_address(user["id"], addr_id, data)
    return jsonify({"ok": ok, "message": msg, "address": row}), (200 if ok else 400)


@bp.delete("/addresses/<int:addr_id>")
def api_delete_address(addr_id):
    data = request.get_json(silent=True) or {}
    user = _current_user(data)
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再删除地址"}), 401
    ok, msg = delete_address(user["id"], addr_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@bp.post("/addresses/<int:addr_id>/default")
def api_set_default_address(addr_id):
    data = request.get_json(silent=True) or {}
    user = _current_user(data)
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再操作"}), 401
    ok, msg, row = set_default_address(user["id"], addr_id)
    return jsonify({"ok": ok, "message": msg, "address": row}), (200 if ok else 400)
