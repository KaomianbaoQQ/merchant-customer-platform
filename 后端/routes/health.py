"""健康检查：自检数据库连接与建表状态。"""
from flask import Blueprint, jsonify

from 售后.order import ensure_tables
from 售后.chat import ensure_chat_table
from 物流.repository import ensure_logistics
from 公共 import inventory

bp = Blueprint("health", __name__, url_prefix="/api")


@bp.get("/health")
def api_health():
    ok_order, msg_order = ensure_tables()
    ok_logi, msg_logi = ensure_logistics()
    ok_chat, msg_chat = ensure_chat_table()
    ok_stock, msg_stock = inventory.ensure_tables()
    return jsonify({
        "ok": ok_order and ok_logi and ok_stock,
        "order": msg_order,
        "logistics": msg_logi,
        "chat": msg_chat,
        "inventory": msg_stock,
    })
