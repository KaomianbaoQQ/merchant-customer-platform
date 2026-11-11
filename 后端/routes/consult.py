"""在线客服（商品咨询）接口。"""
from flask import Blueprint, jsonify, request

from 客服.auto_reply_service import handle_chat_message

bp = Blueprint("consult", __name__, url_prefix="/api")


@bp.post("/consult")
def api_consult():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "请输入咨询内容"}), 400

    user = data.get("user") or {}
    product = data.get("product")
    merchant_id = str(data.get("merchant_id") or "1")
    merchant_name = str(data.get("merchant_name") or "商家")

    result = handle_chat_message(
        message,
        product=product,
        user=user,
        merchant_id=merchant_id,
        merchant_name=merchant_name,
    )

    return jsonify({
        "ok": result.get("ok"),
        "reply": result.get("bot") or "",
        "needs_human": result.get("needs_human", False),
        "error": result.get("error"),
    })
