"""平台（管理员）接口：/api/platform/*

只保留一个平台管理员账号（售后/admin.json，默认 admin / 12345）。
登录后签发令牌（见 公共/admin_auth.py），前端放在
Authorization: Bearer <token> 或 X-Platform-Token。

该令牌同时是「管理员身份」的唯一凭据：客户侧接口（订单 / 物流 / 退换货等）
也认它，带上有效令牌即以管理员身份处理，不带则一律按普通用户。

能力：总览、商家管控（停用 / 恢复、禁言、罚款）、商品下架 / 恢复、
      顾客与商家信息、全部人机 / 人工会话、平台与商家对话、平台仲裁处理、
      商家评价、外部真实快递查询。
"""
from flask import Blueprint, jsonify, request

from 公共 import store
from 公共 import platform_state
from 公共.admin_auth import issue_token, revoke_token, verify_token
from 物流.repository import get_logistics
from 物流.tracking_api import is_external_waybill, query as tracking_query
from 售后.config import verify_admin
from 售后.returns import (
    list_arbitrations, resolve_arbitration, search_returns,
    get_return, get_order_with_logistics,
)
from 售后.order import base_goodsname
from 售后 import platform as pf

bp = Blueprint("platform_admin", __name__, url_prefix="/api/platform")

def _token_from_request():
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Platform-Token")
            or request.args.get("token")
            or (request.get_json(silent=True) or {}).get("token")
            or "")

def _require_admin():
    if not verify_token(_token_from_request()):
        return jsonify({"ok": False, "error": "请先登录平台管理员账号", "code": "NO_AUTH"}), 401
    return None

@bp.post("/login")
def api_platform_login():
    data = request.get_json(silent=True) or {}
    ok, admin, msg = verify_admin(
        data.get("name") or data.get("username"), data.get("password")
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 401
    token = issue_token(admin)
    return jsonify({"ok": True, "admin": admin, "token": token})

@bp.post("/logout")
def api_platform_logout():
    revoke_token(_token_from_request())
    return jsonify({"ok": True})

@bp.get("/me")
def api_platform_me():
    err = _require_admin()
    if err:
        return err
    admin = verify_token(_token_from_request())
    return jsonify({"ok": True, "admin": admin})

@bp.get("/overview")
def api_platform_overview():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "overview": pf.overview()})

@bp.get("/merchants")
def api_platform_merchants():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "merchants": pf.list_merchants()})

@bp.get("/merchants/<mid>")
def api_platform_merchant_detail(mid):
    err = _require_admin()
    if err:
        return err
    detail = pf.merchant_detail(mid)
    if not detail:
        return jsonify({"ok": False, "error": "商家不存在"}), 404
    return jsonify({"ok": True, "detail": detail})

@bp.post("/merchants/<mid>/state")
def api_platform_merchant_state(mid):
    """停用 / 恢复商家账号，或禁言 / 解除禁言。

    - disabled: 停用时自动下架该商家全部在售商品；恢复时把这批被下架的商品恢复上架。
    - muted:    按天禁言，请求体带 days（>0）；days 缺省为 1 天，传 value=false 解除。
    """
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    key = str(body.get("key") or "").strip()
    if key not in ("disabled", "muted"):
        return jsonify({"ok": False, "error": "只支持 disabled（停用）/ muted（禁言）"}), 400

    if key == "disabled":
        value = bool(body.get("value"))
        if value:
            # 停用前先记录「当前在售」的商品，并全部下架
            from 公共 import store as _store
            offline_pids = []
            for p in _store.list_products(owner_id=mid, include_offline=True):
                pid = str(p.get("id") or "").strip()
                if pid and not platform_state.is_product_offline(pid):
                    offline_pids.append(pid)
                    platform_state.set_product_offline(pid, True, "商家被平台停用，自动下架")
            state = platform_state.set_merchant_disabled(mid, True, offline_pids)
            label = "已停用（已下架全部在售商品）"
        else:
            # 恢复账号：把停用时刻在售、被自动下架的商品恢复上架
            prev = platform_state.merchant_state(mid)
            pids = list(prev.get("disabled_offline_pids") or [])
            state = platform_state.set_merchant_disabled(mid, False)
            for pid in pids:
                platform_state.set_product_offline(pid, False)
            label = "已恢复账号" + (f"（已重新上架 {len(pids)} 件商品）" if pids else "")
        return jsonify({"ok": True, "state": state, "message": f"商家 {mid} {label}"})

    days = int(body.get("days") or 1)
    state = platform_state.set_merchant_muted(mid, days if bool(body.get("value")) else 0)
    label = f"已禁言 {state['muted_days']} 天" if state["muted"] else "已解除禁言"
    return jsonify({"ok": True, "state": state, "message": f"商家 {mid} {label}"})

@bp.get("/merchants/<mid>/fines")
def api_platform_merchant_fines(mid):
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "fines": platform_state.list_fines(mid),
                    "total": platform_state.fine_total(mid),
                    "balance": platform_state.get_balance(mid)})

@bp.post("/merchants/<mid>/fines")
def api_platform_add_fine(mid):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = str(body.get("merchant_name") or "").strip()
    if not name:
        name = next((m["shop_name"] or m["name"] for m in pf.list_merchants()
                     if m["id"] == str(mid)), "")
    ok, msg, entry = platform_state.add_fine(mid, name, body.get("amount"), body.get("reason"))
    return jsonify({"ok": ok, "message": msg, "fine": entry,
                    "total": platform_state.fine_total(mid),
                    "balance": platform_state.get_balance(mid)}), (200 if ok else 400)

@bp.get("/merchants/<mid>/balance")
def api_platform_merchant_balance(mid):
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "balance": platform_state.get_balance(mid),
                    "ledger": platform_state.list_ledger(mid)})

# 商品：下架 / 恢复
@bp.get("/products")
def api_platform_products():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "products": pf.list_products()})

@bp.post("/products/<pid>/offline")
def api_platform_offline_product(pid):
    err = _require_admin()
    if err:
        return err
    product = store.get_product(pid, include_offline=True)
    if not product:
        return jsonify({"ok": False, "error": "商品不存在"}), 404
    body = request.get_json(silent=True) or {}
    offline = bool(body.get("offline"))
    state = platform_state.set_product_offline(pid, offline, body.get("reason"))
    msg = f"已下架商品「{product.get('name')}」" if offline else f"已恢复上架商品「{product.get('name')}」"
    return jsonify({"ok": True, "state": state, "message": msg})

@bp.get("/users")
def api_platform_users():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "users": pf.list_users()})

@bp.get("/users/<uid>")
def api_platform_user_detail(uid):
    err = _require_admin()
    if err:
        return err
    detail = pf.user_detail(uid)
    if not detail:
        return jsonify({"ok": False, "error": "顾客不存在"}), 404
    return jsonify({"ok": True, "detail": detail})

# 会话（人机 / 人工）
@bp.get("/conversations")
def api_platform_conversations():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "conversations": pf.list_all_conversations()})

@bp.get("/conversations/search")
def api_platform_conversations_search():
    """聊天记录查询：平台侧按关键词与时间范围搜索全部会话（含售后转人工）。"""
    err = _require_admin()
    if err:
        return err
    args = request.args
    return jsonify({
        "ok": True,
        "results": store.search_threads(
            keyword=args.get("q") or "",
            since=args.get("since") or "",
            until=args.get("until") or "",
        ),
    })

@bp.get("/conversations/thread")
def api_platform_thread():
    err = _require_admin()
    if err:
        return err
    merchant_id = str(request.args.get("merchant_id") or "platform").strip() or "platform"
    user_id = str(request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "缺少 user_id"}), 400
    # 先标已读再取会话：返回的消息里已读标记就是最新的（顾客那条会显示「已读」）
    store.mark_read(merchant_id, user_id, "merchant")
    store.mark_human_ack(merchant_id, user_id)
    thread = store.get_thread(merchant_id, user_id)
    return jsonify({"ok": True, "thread": thread})

@bp.post("/conversations/reply")
def api_platform_conversation_reply():
    """平台以客服身份在某条会话里发消息（用于介入顾客 / 商家的对话）。"""
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    merchant_id = str(body.get("merchant_id") or "platform").strip() or "platform"
    user_id = str(body.get("user_id") or "").strip()
    text = str(body.get("text") or "").strip()
    if not user_id or not text:
        return jsonify({"ok": False, "error": "缺少 user_id 或消息内容"}), 400
    message = store.append_message(
        merchant_id, user_id, None, "merchant", text,
        merchant_name=body.get("merchant_name") or "平台客服",
    )
    return jsonify({"ok": True, "message": message})

# 平台 ↔ 商家 对话
@bp.get("/chats")
def api_platform_chats():
    """平台与各商家的一对一会话列表（含未读）。"""
    err = _require_admin()
    if err:
        return err
    merchant_names = {m["id"]: (m["shop_name"] or m["name"]) for m in pf.list_merchants()}
    items = []
    for m in pf.list_merchants():
        thread = store.get_thread(m["id"], pf.PLATFORM_USER_ID)
        messages = thread.get("messages") or []
        last = messages[-1] if messages else {}
        items.append({
            "merchant_id": m["id"],
            "merchant_name": merchant_names.get(m["id"]) or m["name"],
            "disabled": m["disabled"],
            "muted": m["muted"],
            "last_message": last.get("text", ""),
            "last_role": last.get("role", ""),
            "last_time": last.get("time", ""),
            "unread": int(thread.get("unread_user") or 0),
            "message_count": len(messages),
        })
    items.sort(key=lambda x: str(x.get("last_time") or ""), reverse=True)
    return jsonify({"ok": True, "chats": items})

@bp.get("/chats/thread")
def api_platform_chat_thread():
    err = _require_admin()
    if err:
        return err
    mid = str(request.args.get("merchant_id") or "").strip()
    if not mid:
        return jsonify({"ok": False, "error": "缺少 merchant_id"}), 400
    thread = pf.platform_thread(mid)
    return jsonify({"ok": True, "thread": thread})

@bp.post("/chats/reply")
def api_platform_chat_reply():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    mid = str(body.get("merchant_id") or "").strip()
    if not mid:
        return jsonify({"ok": False, "error": "缺少 merchant_id"}), 400
    name = next((m["shop_name"] or m["name"] for m in pf.list_merchants()
                 if m["id"] == mid), "")
    ok, msg, message = pf.platform_reply(mid, body.get("text"), name)
    return jsonify({"ok": ok, "message": msg, "data": message}), (200 if ok else 400)

@bp.get("/arbitrations")
def api_platform_arbitrations():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "arbitrations": list_arbitrations()})

@bp.get("/arbitrations/<rid>")
def api_platform_arbitration_detail(rid):
    """仲裁详情：一并返回仲裁单、对应商品、双方（顾客 ↔ 商家）对话，
    供平台在「仲裁处理」里查看顾客理由 / 提交图片 / 商品界面 / 双方对话后判定。"""
    err = _require_admin()
    if err:
        return err
    rid = str(rid or "").strip()
    found, row = get_return(rid)
    if not found:
        return jsonify({"ok": False, "error": "仲裁单不存在"}), 404

    # 商品：优先按订单里的 product_id 找，找不到再按商品名兜底匹配
    product = None
    try:
        order = get_order_with_logistics(row.get("order_id"))
        if order and order.get("product_id"):
            product = store.get_product(order["product_id"], include_offline=True)
    except Exception:
        product = None
    if not product:
        target = base_goodsname(row.get("goodsname"))
        for p in store.list_products(include_offline=True):
            if base_goodsname(p.get("name")) == target:
                product = p
                break

    # 双方对话：商家(owner_id) ↔ 顾客(user_id) 的咨询 / 沟通记录
    owner_id = str(row.get("owner_id") or "")
    user_id = str(row.get("user_id") or "")
    thread = {}
    if owner_id and user_id:
        try:
            thread = store.get_thread(owner_id, user_id) or {}
        except Exception:
            thread = {}
    return jsonify({
        "ok": True,
        "arbitration": row,
        "product": product,
        "thread": thread,
    })

@bp.get("/aftersales")
def api_platform_aftersales():
    """按关键词查售后单（含「平台仲裁中」），便于仲裁期间核查进度。

    参数：keyword（售后单号 / 原订单号 / 顾客姓名 / 商品名，可空）、
          status（如 arbitrating，可空）。
    """
    err = _require_admin()
    if err:
        return err
    return jsonify({
        "ok": True,
        "aftersales": search_returns(
            keyword=request.args.get("keyword"),
            status=request.args.get("status"),
        ),
    })

@bp.post("/arbitrations/<rid>")
def api_platform_resolve_arbitration(rid):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ok, msg, data = resolve_arbitration(
        rid, favor_user=bool(body.get("favor_user", True)), note=body.get("note")
    )
    return jsonify({"ok": ok, "message": msg, "arbitration": data}), (200 if ok else 400)

@bp.get("/reviews")
def api_platform_reviews():
    err = _require_admin()
    if err:
        return err
    return jsonify({"ok": True, "reviews": pf.list_all_reviews()})

# 真实快递查询（外部单号，仅平台管理员）
@bp.get("/tracking")
def api_platform_tracking():
    """查外部真实快递单号（非 7 位），走 uapis.cn 真实接口。

    参数：waybill（必填）、phone（选填，收件人手机尾号 4 位，顺丰等需要）。
    """
    err = _require_admin()
    if err:
        return err
    waybill = str(request.args.get("waybill") or "").strip()
    if not waybill:
        return jsonify({"ok": False, "error": "请输入快递单号"}), 400

    # 7 位是本系统内部运单，仍走本地物流表
    if not is_external_waybill(waybill):
        found, row = get_logistics(waybill)
        if not found:
            return jsonify({"ok": False, "error": "未查询到该运单号（7 位为本系统运单）"}), 404
        return jsonify({
            "ok": True,
            "source": "local",
            "tracking": {
                "waybill": row.get("waybill"),
                "carrier": row.get("carrier") or "测试商城速运",
                "status_text": row.get("status_text"),
                "status_step": row.get("status_step", 0),
                "shipped_at": row.get("shipped_at"),
                "estimated_arrival": row.get("estimated_arrival"),
                "address": row.get("address"),
                "user_name": row.get("user_name"),
                "user_id": row.get("user_id"),
                "tracks": [],
            },
        })

    ok, msg, data = tracking_query(waybill, phone=request.args.get("phone"))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 404
    return jsonify({"ok": True, "source": "uapi", "tracking": data})


# ---------------------------------------------------------------- 商户注册审核 / 操作留痕
@bp.get("/merchant-applications")
def api_platform_applications():
    """平台客服查看商户注册申请（默认全部，可带 status=pending 只看待审）。"""
    err = _require_admin()
    if err:
        return err
    from 公共.accounts import list_merchant_applications
    status = str(request.args.get("status") or "").strip() or None
    rows = list_merchant_applications(status=status, limit=request.args.get("limit") or 200)
    return jsonify({"ok": True, "applications": rows})


@bp.post("/merchant-applications/<mid>/review")
def api_platform_review_application(mid):
    """平台客服确认 / 驳回商户注册申请：action = approve / reject。"""
    err = _require_admin()
    if err:
        return err
    from .. import access_control
    from 公共.accounts import review_merchant
    data = request.get_json(silent=True) or {}
    admin = verify_token(_token_from_request()) or {}
    ok, info, msg = review_merchant(
        mid,
        data.get("action"),
        reviewer=admin.get("name") or "平台客服",
        note=data.get("note"),
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    # 审核结果立即生效：清掉身份缓存，避免旧状态最多残留数秒
    access_control.invalidate_identity_cache()
    return jsonify({"ok": True, "application": info, "message": msg})


@bp.get("/audit-logs")
def api_platform_audit_logs():
    """查看全站操作留痕（支持 role / actor_id / path 过滤）。"""
    err = _require_admin()
    if err:
        return err
    from .. import access_control
    rows = access_control.query_audit_logs(
        limit=request.args.get("limit") or 200,
        role=request.args.get("role") or None,
        actor_id=request.args.get("actor_id") or None,
        path=request.args.get("path") or None,
    )
    return jsonify({"ok": True, "logs": rows})