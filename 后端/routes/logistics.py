"""物流查询接口。

运单号分两类：
    · 7 位数字 —— 本系统内部运单（商家发货 / 平台生成的退货单），查 cs.logistics 表，
      按发货时间动态计算状态，并按物权校验归属；
    · 其他 —— 外部真实快递单号。本页只处理 7 位运单号，外部单号一律拒绝，
      请到平台管理页「快递查询」用平台管理员 token 查询（/api/platform/tracking）。

物权校验：除 admin 外，禁止查询别人的物流。
身份由前端在请求中携带 user_id / user_name，经 resolve_user 与数据库核对；
admin（is_admin）可任意查询，普通用户只能查询属于自己的物流。
"""
from flask import Blueprint, jsonify, request

from 物流.repository import get_logistics, get_logistics_by_user
from 物流.tracking_api import is_external_waybill
from ..common import resolve_user

bp = Blueprint("logistics", __name__, url_prefix="/api")

def _identity():
    """从前端参数解析并校验当前用户。返回 (user, err_response)。"""
    user = resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })
    if user is None:
        return None, (jsonify({"ok": False, "error": "请先登录后再查询物流"}), 401)
    return user, None

@bp.get("/logistics")
def api_logistics_list():
    user, err = _identity()
    if err:
        return err
    target = user["id"]
    if not user.get("is_admin"):
        # 非 admin 只能查自己的；若前端硬传别人的 user_id，视为越权
        arg = str(request.args.get("user_id") or "").strip()
        if arg and arg != user["id"]:
            return jsonify({"ok": False, "error": "无权查看他人的物流记录"}), 403
    return jsonify({"ok": True, "list": get_logistics_by_user(target)})

@bp.get("/logistics/<waybill>")
def api_logistics(waybill):
    user, err = _identity()
    if err:
        return err

    if is_external_waybill(waybill):
        return jsonify({
            "ok": False,
            "error": "本页仅支持本系统的 7 位运单号；外部真实快递单号请到平台管理页「快递查询」中查询",
        }), 403

    found, row = get_logistics(waybill)
    if not found:
        return jsonify({"message": "未查询到该物流单号"}), 404

    if not user.get("is_admin"):
        owner = str(row.get("user_id") or "").strip()
        if owner and owner != user["id"]:
            return jsonify({"ok": False, "error": "无权查看他人的物流信息"}), 403

    # 前端拿到时间戳后也会每秒本地重算，保证「实时」。
    return jsonify({
        "ok": True,
        "code": row["waybill"],
        "carrier": row.get("carrier") or "测试商城速运",
        "shippedAt": row.get("shipped_at"),
        "estimatedArrival": row.get("estimated_arrival"),
        "statusStep": row.get("status_step", 0),
        "statusText": row.get("status_text") or "已下单",
        "address": row.get("address"),
        "items": row.get("order_ids"),
        "source": "local",
    })
