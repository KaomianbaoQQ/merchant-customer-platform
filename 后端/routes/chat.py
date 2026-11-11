"""顾客 ↔ 商家沟通接口（客户前端）。

顾客在「联系商家」里发送消息，先由 Dify 知识库自动回答（bot 消息），
不再依赖商家实时回复；当顾客累计说出「人工」满 3 次后，会话标记为
needs_human，商家在商户后台看到高亮 + 完整对话记录并可真人回复。
会话按「商户 + 顾客」隔离：顾客在某个商品页咨询时，merchant_id 即为
该商品所属商家。
"""
from flask import Blueprint, jsonify, request

from 公共 import store
from 客服.auto_reply_service import handle_chat_message

bp = Blueprint("merchant_chat", __name__, url_prefix="/api")

@bp.post("/chat")
def api_send():
    data = request.get_json(silent=True) or {}
    text = str(data.get("message") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请输入消息内容"}), 400
    merchant_id = str(data.get("merchant_id") or "platform").strip() or "platform"
    merchant_name = str(data.get("merchant_name") or "").strip()
    user = data.get("user") or {}
    result = handle_chat_message(
        text,
        product=data.get("product"),
        user=user,
        merchant_id=merchant_id,
        merchant_name=merchant_name,
    )
    if not result.get("ok"):
        return jsonify(result), 400
    # 顾客此刻就在客服面板里：刚生成的机器人 / 商家消息直接算「已读」
    store.mark_read(merchant_id, str(user.get("id") or "guest").strip() or "guest", "user")
    return jsonify(result)

@bp.get("/chat")
def api_poll():
    merchant_id = str(request.args.get("merchant_id") or "platform").strip() or "platform"
    user_id = str(request.args.get("user_id") or "guest").strip() or "guest"
    store.mark_read(merchant_id, user_id, "user")
    thread = store.get_thread(merchant_id, user_id)
    return jsonify({
        "ok": True,
        "messages": thread.get("messages") or [],
        "user_name": thread.get("user_name") or "",
        "needs_human": bool(thread.get("needs_human")),
        # 商家真人已回复过：会话保持人工通道（前端据此不再重复提示「已转人工」）
        "human_handled": bool(thread.get("human_handled")),
    })
