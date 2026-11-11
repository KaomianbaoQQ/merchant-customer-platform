"""退换货接口（退货 / 换货分开）。"""
from flask import Blueprint, jsonify, request

from 售后.returns import (
    create_return,
    get_refund_ledger,
    get_return,
    get_returns_by_user,
    mark_return_shipped,
    request_arbitration,
    update_return_waybill,
)
from ..common import resolve_user

bp = Blueprint("returns", __name__, url_prefix="/api")


@bp.post("/returns")
def api_create_return():
    """申请退货 / 换货：return_type = return（退货） / exchange（换货）。"""
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再申请退换货"}), 401

    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"ok": False, "error": "缺少订单号"}), 400

    return_type = str(data.get("return_type") or "return").strip().lower()
    ok, msg, ret = create_return(
        order_id, user,
        reason=str(data.get("reason") or "").strip(),
        return_type=return_type,
        quantity=data.get("quantity"),
    )
    return jsonify({"ok": ok, "message": msg, "return": ret}), (200 if ok else 400)


@bp.post("/returns/<return_id>/waybill")
def api_return_waybill(return_id):
    """把客服给出的快递单号填入订单信息。"""
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再操作"}), 401

    ok, msg, ret = update_return_waybill(
        return_id, data.get("waybill"), user_id=user["id"]
    )
    return jsonify({"ok": ok, "message": msg, "return": ret}), (200 if ok else 400)


@bp.post("/returns/<return_id>/shipped")
def api_return_shipped(return_id):
    """用户点「我已发出」，可同时带上快递单号。"""
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再操作"}), 401

    ok, msg, ret = mark_return_shipped(
        return_id, user_id=user["id"], waybill=data.get("waybill")
    )
    return jsonify({"ok": ok, "message": msg, "return": ret}), (200 if ok else 400)


@bp.get("/returns")
def api_list_returns():
    user = resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401
    return jsonify({"ok": True, "list": get_returns_by_user(user["id"])})


@bp.post("/returns/<return_id>/arbitrate")
def api_return_arbitrate(return_id):
    """用户对被商家拒绝的退换货申请平台仲裁。"""
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再操作"}), 401

    ok, msg, ret = request_arbitration(return_id, user_id=user["id"])
    return jsonify({"ok": ok, "message": msg, "return": ret}), (200 if ok else 400)


@bp.get("/returns/<return_id>")
def api_get_return(return_id):
    found, row = get_return(return_id)
    if not found:
        return jsonify({"message": "未查询到该退换货单"}), 404
    return jsonify({"ok": True, "return": row})


@bp.get("/refund-ledger")
def api_refund_ledger():
    """退款流水（平台管理视角）。需管理员令牌。"""
    user = resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })
    if user is None or not user.get("is_admin"):
        return jsonify({"ok": False, "error": "需要管理员权限"}), 403
    rows = get_refund_ledger(limit=int(request.args.get("limit") or 200))
    total = sum(float(r.get("amount") or 0) for r in rows)
    return jsonify({"ok": True, "ledger": rows, "total_amount": round(total, 2)})
