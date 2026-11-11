"""售后客服接口（需登录）。"""
from flask import Blueprint, jsonify, request

from 售后.chat import (ensure_chat_table, mark_session_read, new_session_id,
                       save_message, get_recent_messages)
from 售后.service import handle_after_sales
from ..common import resolve_user

bp = Blueprint("after_sales", __name__, url_prefix="/api")

@bp.post("/after-sales")
def api_after_sales():
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再使用售后客服"}), 401

    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "请输入内容"}), 400

    ensure_chat_table()
    session_id = str(data.get("session_id") or "").strip() or new_session_id(user["id"])
    save_message(session_id, user["id"], user["name"], "user", message)

    result = handle_after_sales(user, message, session_id=session_id)
    # 顾客就在售后面板里：客服刚给的那条回复直接算「已读」
    mark_session_read(session_id)
    result["session_id"] = session_id
    return jsonify(result)

@bp.get("/after-sales")
def api_after_sales_history():
    """恢复售后客服历史消息（刷新后调用）。"""
    user = resolve_user(data_from_query(request.args))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401

    ensure_chat_table()
    session_id = str(request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"ok": True, "messages": [], "session_id": None})

    mark_session_read(session_id)
    rows = get_recent_messages(session_id, limit=50)
    messages = []
    for r in rows:
        role = r.get("role") or "user"
        messages.append({
            "id": f"{session_id}-{len(messages)}",
            "role": "user" if role == "user" else "bot",
            "text": r.get("content") or "",
            "read_user": bool(r.get("read_user", role != "user")),
            "read_agent": bool(r.get("read_agent", role != "user")),
        })
    return jsonify({"ok": True, "messages": messages, "session_id": session_id})

def data_from_query(args):
    """把 query string 转成 resolve_user 需要的 dict。"""
    return {
        "id": args.get("user_id"),
        "name": args.get("user_name"),
    }
