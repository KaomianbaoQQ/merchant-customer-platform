"""合并客服 —— 知识库自动回答 + 退换货分流 + 物流查询 + 满 3 次「人工」转接商家。

整合后：顾客在「联系商家」里发消息，一律先由 Dify「商品咨询」工作流
（基于商品信息知识库）自动回答并落盘为 bot 消息，不依赖商家实时回复。
只有当顾客在该会话里累计说出「人工 / 转人工 / 真人客服 …」满
HUMAN_RE 命中 3 次，才把会话标记为 needs_human（商家后台高亮 + 拿到完整
对话记录并可回复）。

分流（都在调 Dify 之前确定性处理，避免 LLM 误判）：
    1. 退换货/退款意图 → 固定话术，引导到「售后客服」入口
    2. 物流查询 → 直接查「当前咨询商品 + 当前顾客」的订单与运单状态作答
       （后端保底；同时把这段订单物流上下文拼进 user_input，
        Dify 工作流的「物流查询」分支可直接引用）

对外主函数：
    handle_chat_message(text, product, user, merchant_id, merchant_name) -> dict
"""
import re

from 公共 import store
from 客服.workflow import ask_product_service

# 与前端原 HUMAN_RE 保持一致：命中即视为想转人工
HUMAN_RE = re.compile(r"(人工|真人|转人工|人工客服|人工服务|找客服|要客服|客服在吗|真人客服)")
HUMAN_TRIGGER_TIMES = 3
# 连续意图不明达到该次数后，主动询问是否转人工
UNCLEAR_TRIGGER = 2
ASK_HUMAN_REPLY = "我暂时没太理解你的意思。是否需要为你转接人工客服？回复「是」「人工」或「需要」即可接通。"
# 用户确认转人工的表达
CONFIRM_HUMAN_RE = re.compile(r"(^是的?$|^好$|^需要$|^要$|^嗯$|^ok$|人工|转人工|好的|可以|行)")
# 机器人回复里属于「没听懂 / 请用户澄清」的话术，视为意图不明
UNCLEAR_REPLY_RE = re.compile(
    r"(请问.*想咨询|请提供|换个说法|不太理解|没太理解|没看懂|不太明白|不明白|"
    r"请说具体|请描述|相关问题需要咨询|只能解答|请咨询商品|哪款商品)"
)

TRANSFER_REPLY = "已为您转接人工客服，商家会尽快回复，请稍候。"
HISTORY_LIMIT = 10

# 退换货 / 退款意图关键词：命中即引导用户到「售后客服」入口处理
# （排除「退出」这类无关说法）
RETURN_INTENT_RE = re.compile(
    r"(退货|退换货|退换|换货|退款|退钱|退费|只退款|申请退|退掉|退单|"
    r"退回|换新|更换|以旧换新|退一个|退两个|换一个|换一台)"
)
RETURN_TRANSFER_REPLY = (
    "检测到你有退换货/退款需求，这部分由「售后客服」专门处理。\n"
    "请点击页面下方的「售后客服」按钮进入售后通道，会有专属客服为你办理。"
)

# 物流问法：命中即查「当前商品 + 当前顾客」的订单与运单状态
LOGISTICS_RE = re.compile(
    r"(物流|快递|运单|单号|发货|发了没|发了吗|到哪|到哪里|多久到|多长时间到|"
    r"什么时候到|几天到|何时到|签收|收到|收货|配送|派送|运输|寄出|在路上|到货)"
)


def _order_line(o):
    """一行订单物流摘要（人话）。"""
    parts = [f"订单号 {o.get('id')}", f"物流状态 {o.get('status_text') or '—'}"]
    if o.get("waybill"):
        parts.append(f"运单号 {o['waybill']}")
    if o.get("shipped_at"):
        parts.append(f"发货时间 {o['shipped_at']}")
    if o.get("estimated_arrival"):
        parts.append(f"预计送达 {o['estimated_arrival']}")
    return "｜".join(parts)


def product_orders_for_user(user_id, product):
    """该顾客在当前咨询商品上的订单（按时间倒序）；查不到返回 []。"""
    pid = str((product or {}).get("id") or "").strip()
    uid = str(user_id or "").strip()
    if not pid or not uid or uid.startswith("guest-"):
        return []
    try:
        from 售后.order import get_orders_by_user
        orders = get_orders_by_user(uid)
    except Exception:
        return []
    hit = [o for o in orders if str(o.get("product_id") or "") == pid]
    if hit:
        return hit
    # 老订单没有 product_id 时按商品名兜底
    name = str((product or {}).get("name") or "").strip()
    if not name:
        return []
    from 售后.order import base_goodsname
    return [o for o in orders if base_goodsname(o.get("goodsname")) == base_goodsname(name)]


def answer_product_logistics(user_id, product):
    """按「当前咨询商品」回答物流状态；没有该商品的订单时返回 None（交给 Dify）。"""
    orders = product_orders_for_user(user_id, product)
    if not orders:
        return None
    name = str((product or {}).get("name") or "这件商品")
    if len(orders) == 1:
        o = orders[0]
        lines = [f"「{name}」这笔订单的物流：", _order_line(o)]
        if not o.get("waybill"):
            lines.append("商家还没发货，发货后就能用运单号实时查物流了。")
        return "\n".join(lines)
    lines = [f"你在「{name}」上共有 {len(orders)} 笔订单："]
    lines += [_order_line(o) for o in orders[:5]]
    lines.append("想看某一笔的详细物流，告诉我订单号即可。")
    return "\n".join(lines)


def count_human_hits(thread):
    """从最近一条「顾客消息」往前数，连续命中「人工」关键词的条数。

    只看 role=user 的消息，机器人 / 商家回复不打断连续：顾客连说 3 次「人工」
    就是 3 次，中间说了别的内容就归零（与售后的连续判定口径一致）。
    若按累计统计，转人工后计数会永远 >=3，导致顾客重新进来也一直被当作「要人工」。
    """
    hits = 0
    for m in reversed(thread.get("messages") or []):
        if m.get("role") != "user":
            continue
        if HUMAN_RE.search(str(m.get("text") or "")):
            hits += 1
        else:
            break
    return hits


def build_conversation_history(thread):
    """把最近几条 user / bot / merchant 消息转成 [{role, text}]，供 Dify 判断意图。"""
    history = []
    for m in (thread.get("messages") or [])[-HISTORY_LIMIT:]:
        role = m.get("role")
        if role in ("user", "bot", "merchant"):
            history.append({"role": role, "text": str(m.get("text") or "")})
    return history


def handle_chat_message(text, product, user, merchant_id, merchant_name):
    """
    处理一条来自顾客的客服消息。

    参数：
        text           本条消息文本
        product        当前浏览商品 dict（可为 None）
        user           {"id","name"}（可能为游客）
        merchant_id    商品所属商户 id
        merchant_name  商铺名

    返回 dict：
        ok             bool
        message        已落盘的原始用户消息（含 id）
        bot            str|None  知识库/转人工的回复文本
        bot_message_id str|None  落盘的 bot 消息 id
        needs_human    bool      本会话是否已转入工（3 次人工触发）
        error          str|None
    """
    user_id = str((user or {}).get("id") or "guest").strip() or "guest"
    user_name = str((user or {}).get("name") or "").strip()

    thread = store.get_thread(merchant_id, user_id)
    # 只有「顾客正在等人工」（needs_human）时机器人才暂停自动回答。
    # 商家回复过（human_handled=True）之后本次人工已结束，机器人恢复作答——
    # 这样顾客退出浏览器再进来，仍然有机器人应答，不会被永久静音。
    waiting_human = bool(thread.get("needs_human"))

    message = store.append_message(
        merchant_id, user_id, user_name, "user", text, merchant_name,
        suppress_unread=not waiting_human,
    )
    if not message:
        return {"ok": False, "error": "消息为空", "message": None,
                "bot": None, "bot_message_id": None, "needs_human": waiting_human}

    # 正在等人工：由商家真人回复，机器人暂不插话
    if waiting_human:
        return {"ok": True, "message": message, "bot": None,
                "bot_message_id": None,
                "needs_human": True, "error": None}

    # 退换货 / 退款意图分流：商品咨询不处理售后流程，引导用户到「售后客服」入口
    if RETURN_INTENT_RE.search(text):
        bot = store.append_message(
            merchant_id, user_id, user_name, "bot", RETURN_TRANSFER_REPLY, merchant_name
        )
        return {"ok": True, "message": message, "bot": RETURN_TRANSFER_REPLY,
                "bot_message_id": bot["id"] if bot else None,
                "needs_human": False, "error": None}

    # 重新读取线程（拿到刚插入的消息）再判断是否转人工
    thread = store.get_thread(merchant_id, user_id)
    if count_human_hits(thread) >= HUMAN_TRIGGER_TIMES:
        store.mark_needs_human(merchant_id, user_id)
        bot = store.append_message(
            merchant_id, user_id, user_name, "bot", TRANSFER_REPLY, merchant_name
        )
        return {"ok": True, "message": message, "bot": TRANSFER_REPLY,
                "bot_message_id": bot["id"] if bot else None,
                "needs_human": True, "error": None}

    # 若上一轮机器人主动询问是否转人工，且用户本次确认，则直接接通人工
    if thread.get("asking_human") and CONFIRM_HUMAN_RE.search(text.strip()):
        store.mark_needs_human(merchant_id, user_id)
        bot = store.append_message(
            merchant_id, user_id, user_name, "bot", TRANSFER_REPLY, merchant_name
        )
        return {"ok": True, "message": message, "bot": TRANSFER_REPLY,
                "bot_message_id": bot["id"] if bot else None,
                "needs_human": True, "error": None}

    # 物流查询分流：直接查「当前咨询商品 + 当前顾客」的订单与运单状态作答。
    # 查不到该商品的订单时返回 None，仍交给 Dify（泛问询如「一般几天发货」由知识库答）。
    if LOGISTICS_RE.search(text):
        reply = answer_product_logistics(user_id, product)
        if reply:
            bot = store.append_message(
                merchant_id, user_id, user_name, "bot", reply, merchant_name
            )
            return {"ok": True, "message": message, "bot": reply,
                    "bot_message_id": bot["id"] if bot else None,
                    "needs_human": False, "error": None}

    # 未被转人工：调用知识库自动回答（把当前商品的订单物流一并上传，供物流分支引用）
    history = build_conversation_history(thread)
    result = ask_product_service(text, product=product, user_id=user_id,
                                 conversation_history=history,
                                 orders=product_orders_for_user(user_id, product),
                                 merchant_id=merchant_id, merchant_name=merchant_name)
    reply = result.get("reply") or "抱歉，暂时无法回答，请稍后再试。"

    # 意图不明：工作流未命中任何分支，或机器人回复属于「请用户澄清」类话术
    unclear = (not result.get("ok")) or bool(UNCLEAR_REPLY_RE.search(reply))
    thread2 = store.get_thread(merchant_id, user_id)
    unclear_count = int(thread2.get("unclear_count") or 0)
    if unclear:
        unclear_count += 1
    else:
        unclear_count = 0

    # 连续意图不明达到阈值：主动询问是否转人工
    asking = False
    if unclear_count >= UNCLEAR_TRIGGER:
        reply = ASK_HUMAN_REPLY
        unclear_count = 0
        asking = True

    store.set_thread_meta(merchant_id, user_id, {
        "unclear_count": unclear_count,
        "asking_human": asking,
    })

    bot = store.append_message(
        merchant_id, user_id, user_name, "bot", reply, merchant_name
    )
    return {"ok": True, "message": message, "bot": reply,
            "bot_message_id": bot["id"] if bot else None,
            "needs_human": False, "error": result.get("error")}