"""聊天记录的 NoSQL 存储（MongoDB）。

售前（顾客 ↔ 商家 / 机器人）与售后（客服对话）的全部聊天记录都存在 MongoDB：
    presale_threads        一条会话一个文档，消息放在 messages 数组里
    after_sales_messages   一条消息一个文档（售后按会话流水查询）

本模块对外函数**签名与返回值都跟原来的实现保持一致**：
    · 售前沿用原 `公共/store.py` 的 6 个函数 → 调用点零改动
    · 售后沿用原 `售后/chat.py` 的 5 个函数 → 调用点零改动

降级策略：MongoDB 连不上时只打印中文告警，不让下单 / 客服主流程报错
（与原实现「保存失败不影响主流程」一致）。读不到数据时返回空结果。
"""
import time
import uuid
from datetime import datetime

import pymongo

from 公共.db_config import MONGO_CONFIG

# 集合名
PRESALE_COLLECTION = "presale_threads"
AFTER_SALES_COLLECTION = "after_sales_messages"

_client = None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _brief(error):
    """把 pymongo 冗长的连接错误压成一行，日志才看得清。"""
    return str(error).split(", Topology Description")[0]


def get_client():
    """MongoClient 单例（自带连接池，全程复用）。连不上也不抛异常。"""
    global _client
    if _client is None:
        _client = pymongo.MongoClient(
            MONGO_CONFIG["uri"],
            serverSelectionTimeoutMS=800,   # 连不上就快速失败，别拖住请求
            connect=False,
        )
    return _client


def _coll(name):
    return get_client()[MONGO_CONFIG["database"]][name]


def ensure_indexes():
    """建索引（幂等）。返回 (是否成功, 提示信息)，供启动自检 / 健康检查调用。"""
    try:
        _coll(PRESALE_COLLECTION).create_index([("merchant_id", 1), ("user_id", 1)], unique=True)
        _coll(PRESALE_COLLECTION).create_index([("merchant_id", 1), ("updated_at", -1)])
        _coll(PRESALE_COLLECTION).create_index([("updated_at", -1)])
        _coll(AFTER_SALES_COLLECTION).create_index([("session_id", 1), ("_id", -1)])
        _coll(AFTER_SALES_COLLECTION).create_index([("user_id", 1)])
        _coll(AFTER_SALES_COLLECTION).create_index([("legacy_id", 1)], sparse=True, unique=True)
        return True, "聊天记录集合已就绪（MongoDB）"
    except Exception as e:
        return False, f"MongoDB 连接失败：{e}"


# ------------------------------------------------------------
# 售前：商户 ↔ 顾客会话（对应原 公共/store.py 的 chat 部分）
# ------------------------------------------------------------
def _thread_key(merchant_id, user_id):
    return "%s::%s" % (str(merchant_id or "platform").strip() or "platform",
                       str(user_id or "guest").strip() or "guest")


def _thread_filter(merchant_id, user_id):
    return {
        "merchant_id": str(merchant_id or "platform").strip() or "platform",
        "user_id": str(user_id or "guest").strip() or "guest",
    }


def append_message(merchant_id, user_id, user_name, role, text, merchant_name="",
                   suppress_unread=False):
    """追加一条消息。role: user（顾客）/ merchant（商家）/ bot（机器人）。

    会话按「商户 + 顾客」隔离：同一个顾客咨询不同商户会生成不同会话。
    suppress_unread=True 时不递增 unread_merchant（普通机器人问答不打扰商家）。
    """
    text = str(text or "").strip()
    if not text:
        return None
    is_bot = role == "bot"
    safe_role = role if role in ("user", "merchant", "bot") else "user"
    message = {
        "id": uuid.uuid4().hex[:12],
        "role": safe_role,
        "text": text,
        "time": _now(),
        # 已读标记：read_user = 顾客侧已读，read_merchant = 商家侧已读（机器人消息算商家侧发出）。
        # 发出消息的那一侧天然是「已读」，对面要等点进这个会话才翻成已读。
        "read_user": safe_role == "user",
        "read_merchant": safe_role in ("merchant", "bot"),
    }
    if is_bot:
        message["is_bot"] = True

    merchant_id = str(merchant_id or "platform").strip() or "platform"
    user_id = str(user_id or "guest").strip() or "guest"
    set_fields = {
        "key": _thread_key(merchant_id, user_id),
        "merchant_id": merchant_id,
        "user_id": user_id,
        "updated_at": message["time"],
    }
    if user_name:
        set_fields["user_name"] = str(user_name).strip()
    if merchant_name:
        set_fields["merchant_name"] = str(merchant_name).strip()

    inc_fields = {}
    if safe_role == "user":
        if not suppress_unread:
            inc_fields["unread_merchant"] = 1
    elif safe_role == "merchant":
        inc_fields["unread_user"] = 1
        # 商家已真人回复：「待人工」标记解除（会话保持人工通道）
        set_fields["needs_human"] = False
        set_fields["human_handled"] = True

    on_insert = {
        "user_name": str(user_name or "").strip() or ("顾客 " + user_id),
        "merchant_name": str(merchant_name or "").strip(),
        "unread_merchant": 0,
        "unread_user": 0,
    }
    # 同一字段不能同时出现在 $set / $inc / $push 与 $setOnInsert（Mongo 会报路径冲突 code 40）：
    # messages 由 $push 建；被 $set / $inc 覆盖的字段也不再放进 $setOnInsert。
    used = set(set_fields) | set(inc_fields) | {"messages"}
    on_insert = {k: v for k, v in on_insert.items() if k not in used}

    update = {"$push": {"messages": message}, "$set": set_fields, "$setOnInsert": on_insert}
    if inc_fields:
        update["$inc"] = inc_fields

    try:
        coll = _coll(PRESALE_COLLECTION)
        coll.update_one(_thread_filter(merchant_id, user_id), update, upsert=True)
        # 商家 / 机器人已经回复 → 之前顾客发的消息算「商家侧已读」（顾客那边不再一直显示未读）。
        # 注意：只改消息上的已读标记，不动 unread_merchant 计数（那是商家自己没看过的角标）。
        if safe_role in ("merchant", "bot"):
            try:
                coll.update_one(
                    _thread_filter(merchant_id, user_id),
                    {"$set": {"messages.$[m].read_merchant": True}},
                    array_filters=[{"m.role": "user", "m.read_merchant": {"$ne": True}}],
                )
            except Exception:
                pass
    except Exception as e:
        # 降级：只告警，消息照常返回，客服/下单流程不受影响
        print(f"⚠️ 聊天记录写入 MongoDB 失败（消息未落库）：{_brief(e)}")
    return message


def mark_needs_human(merchant_id, user_id):
    """把会话标记为「需人工」，并给商家递增一次未读。会话不存在返回 False。

    同时把「已接手」标记清零：新一轮转人工要重新提醒（前端会重新闪动 / 亮横幅）。
    """
    query = _thread_filter(merchant_id, user_id)
    try:
        coll = _coll(PRESALE_COLLECTION)
        res = coll.update_one(query, {"$set": {"needs_human": True, "human_ack": False},
                                      "$inc": {"unread_merchant": 1}})
        if res.matched_count == 0:
            return False
        coll.update_one(dict(query, human_since={"$exists": False}),
                        {"$set": {"human_since": _now()}})
        return True
    except Exception as e:
        print(f"⚠️ 标记转人工失败：{_brief(e)}")
        return False


def mark_human_ack(merchant_id, user_id):
    """标记「已有客服接手这条待人工会话」（平台客服点开会话即算接手）。

    接手后 needs_human 仍为 True（机器人继续让位、会话保持人工通道），
    只是前端不再闪动 / 不再亮强提醒横幅 —— 闪烁只对「没人管」的会话。
    """
    query = _thread_filter(merchant_id, user_id)
    try:
        res = _coll(PRESALE_COLLECTION).update_one(
            dict(query, needs_human=True), {"$set": {"human_ack": True}})
        return res.modified_count > 0
    except Exception as e:
        print(f"⚠️ 标记接手失败：{_brief(e)}")
        return False


def set_thread_meta(merchant_id, user_id, meta):
    """更新会话的自定义元数据字段（如 unclear_count / asking_human）。"""
    try:
        res = _coll(PRESALE_COLLECTION).update_one(_thread_filter(merchant_id, user_id),
                                                   {"$set": dict(meta or {})})
        return res.matched_count > 0
    except Exception as e:
        print(f"⚠️ 更新会话状态失败：{_brief(e)}")
        return False


def get_thread(merchant_id, user_id):
    """取一个会话；不存在或读不到时返回空会话结构。"""
    merchant_id = str(merchant_id or "platform").strip() or "platform"
    user_id = str(user_id or "guest").strip() or "guest"
    try:
        thread = _coll(PRESALE_COLLECTION).find_one(_thread_filter(merchant_id, user_id),
                                                    {"_id": 0})
        if thread:
            thread.setdefault("messages", [])
            # 历史消息（从 chat.json 迁移进来的）没有已读标记：按「发送方已读、接收方未读」补齐，
            # 前端据此显示已读 / 未读，不会因为字段缺失而显示错乱。
            for m in thread["messages"]:
                m.setdefault("read_user", m.get("role") == "user")
                m.setdefault("read_merchant", m.get("role") in ("merchant", "bot"))
            return thread
    except Exception as e:
        print(f"⚠️ 读取会话失败：{_brief(e)}")
    return {
        "key": _thread_key(merchant_id, user_id),
        "merchant_id": merchant_id,
        "user_id": user_id,
        "user_name": "",
        "messages": [],
    }


def list_conversations(merchant_id=None, include_platform=True):
    """会话列表。merchant_id 为空返回全部；否则只返回该商户的会话。"""
    want = None if merchant_id in (None, "") else str(merchant_id)
    try:
        threads = list(_coll(PRESALE_COLLECTION).find({}, {"_id": 0}))
    except Exception as e:
        print(f"⚠️ 读取会话列表失败：{_brief(e)}")
        return []
    items = []
    for thread in threads:
        tid = str(thread.get("merchant_id") or "platform")
        if want is not None and tid != want:
            if not (include_platform and tid == "platform"):
                continue
        messages = thread.get("messages") or []
        last = messages[-1] if messages else {}
        needs = bool(thread.get("needs_human"))
        ack = bool(thread.get("human_ack"))
        items.append({
            "key": thread.get("key"),
            "merchant_id": tid,
            "merchant_name": thread.get("merchant_name") or "",
            "user_id": thread.get("user_id"),
            "user_name": thread.get("user_name"),
            "last_message": last.get("text", ""),
            "last_role": last.get("role", ""),
            "last_time": last.get("time", thread.get("updated_at", "")),
            "unread_merchant": int(thread.get("unread_merchant") or 0),
            "needs_human": needs,
            # 已被客服接手（平台点开会话 / 商家真人回复过）：不再闪动、不计入待处理
            "human_ack": ack,
            "waiting_human": needs and not ack,
            "message_count": len(messages),
        })
    items.sort(key=lambda x: str(x.get("last_time") or ""), reverse=True)
    return items


def search_threads(merchant_id=None, keyword="", since="", until=""):
    """按关键词 + 时间范围搜索会话（商户后台与平台后台共用）。

    keyword：命中「顾客姓名 / 顾客 ID」或「任意一条消息内容」（不区分大小写）；
    since / until：按会话最后时间过滤，格式 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"。
    返回会话摘要 + 命中的消息原文（matched），用于「聊天记录查询」。
    """
    keyword = str(keyword or "").strip()
    since = str(since or "").strip()
    until = str(until or "").strip()

    query = {}
    if merchant_id not in (None, ""):
        query["merchant_id"] = str(merchant_id)
    if since or until:
        rng = {}
        if since:
            rng["$gte"] = since
        if until:
            rng["$lte"] = until
        query["updated_at"] = rng

    try:
        threads = list(_coll(PRESALE_COLLECTION).find(query, {"_id": 0}))
    except Exception as e:
        print(f"⚠️ 搜索聊天记录失败：{_brief(e)}")
        return []

    low = keyword.lower()
    items = []
    for thread in threads:
        messages = thread.get("messages") or []
        matched = []
        if low:
            name_hit = (low in str(thread.get("user_name") or "").lower()
                        or low in str(thread.get("user_id") or "").lower())
            matched = [str(m.get("text") or "") for m in messages
                       if low in str(m.get("text") or "").lower()]
            if not (name_hit or matched):
                continue
        last = messages[-1] if messages else {}
        needs = bool(thread.get("needs_human"))
        ack = bool(thread.get("human_ack"))
        items.append({
            "key": thread.get("key"),
            "merchant_id": str(thread.get("merchant_id") or "platform"),
            "merchant_name": thread.get("merchant_name") or "",
            "user_id": thread.get("user_id"),
            "user_name": thread.get("user_name"),
            "message_count": len(messages),
            "last_message": last.get("text", ""),
            "last_role": last.get("role", ""),
            "last_time": last.get("time", thread.get("updated_at", "")),
            "needs_human": needs,
            "human_ack": ack,
            "waiting_human": needs and not ack,
            "matched": matched[:5],          # 命中的消息原文（各截断由前端处理）
            "matched_count": len(matched),
        })
    items.sort(key=lambda x: str(x.get("last_time") or ""), reverse=True)
    return items


def mark_read(merchant_id, user_id, role):
    """把某一方的未读数清零，并把对面发来的消息标记成「已读」。

    role 为读取方：merchant 读顾客的消息（写 read_merchant），
    user 读商家 / 机器人发来的消息（写 read_user）。点进会话就会调用它，
    所以「点进去 = 对面消息变已读」，没点进去就一直是未读。
    """
    query = _thread_filter(merchant_id, user_id)
    if role == "merchant":
        flag, condition, counter = (
            "read_merchant", {"m.role": "user", "m.read_merchant": {"$ne": True}},
            "unread_merchant")
    else:
        flag, condition, counter = (
            "read_user", {"m.role": {"$in": ["merchant", "bot"]}, "m.read_user": {"$ne": True}},
            "unread_user")
    try:
        coll = _coll(PRESALE_COLLECTION)
        coll.update_one(query, {"$set": {"messages.$[m]." + flag: True}},
                        array_filters=[condition])
        coll.update_one(query, {"$set": {counter: 0}})
    except Exception as e:
        print(f"⚠️ 更新未读状态失败：{_brief(e)}")


# ------------------------------------------------------------
# 售后：客服对话流水（对应原 售后/chat.py 的 cs.chat_log）
# ------------------------------------------------------------
def new_session_id(user_id):
    """生成一次登录的会话ID，如 99-20260910203015"""
    return f"{user_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def save_message(session_id, user_id, user_name, role, content):
    """保存一条对话消息。content 为空则跳过；出错只告警，返回 True/False。

    已读标记：read_user = 顾客侧已读，read_agent = 客服侧已读
    （顾客自己发的是 read_user，客服/机器人的回复是 read_agent；
    客服发出回复时，把之前顾客的消息一并标成「客服已读」）。
    """
    content = (content or "").strip()
    if not content:
        return False
    is_user = role == "user"
    doc = {
        "session_id": str(session_id or ""),
        "user_id": str(user_id),
        "user_name": user_name,
        "role": role,
        "content": content,
        "create_time": datetime.now(),
        "created_at": _now(),
        "read_user": is_user,
        "read_agent": not is_user,
    }
    try:
        coll = _coll(AFTER_SALES_COLLECTION)
        if not is_user:
            # 客服侧已经处理过（生成了回复），把之前的顾客消息标为客服已读
            coll.update_many({"session_id": doc["session_id"], "role": "user",
                              "read_agent": {"$ne": True}}, {"$set": {"read_agent": True}})
        coll.insert_one(doc)
        return True
    except Exception as e:
        print(f"⚠️ 对话记录保存失败：{_brief(e)}")
        return False


def mark_after_sales_read(session_id):
    """顾客点进售后面板：把客服 / 机器人已发出的消息标记为「已读」。"""
    sid = str(session_id or "").strip()
    if not sid:
        return
    try:
        _coll(AFTER_SALES_COLLECTION).update_many(
            {"session_id": sid, "role": {"$ne": "user"}, "read_user": {"$ne": True}},
            {"$set": {"read_user": True}})
    except Exception as e:
        print(f"⚠️ 更新售后已读状态失败：{_brief(e)}")


def save_turn(session_id, user, user_input, reply):
    """保存一问一答（用户 + 客服两条）"""
    save_message(session_id, user["id"], user["name"], "user", user_input)
    save_message(session_id, user["id"], user["name"], "assistant", reply)


def count_recent_consecutive(session_id, match, scan=50):
    """从最近一条「顾客消息」往前数，连续满足 match(text) 的条数。

    客服回复不打断连续（只看 role=user 的消息），因此顾客连说 3 次「人工」
    就是 3 次；中间说了别的就会归零。供售后「连续 3 次人工转人工」判定使用。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return 0
    try:
        rows = list(_coll(AFTER_SALES_COLLECTION)
                    .find({"session_id": sid}, {"_id": 0, "role": 1, "content": 1})
                    .sort([("_id", -1)]).limit(int(scan)))
    except Exception as e:
        print(f"⚠️ 读取最近对话失败：{_brief(e)}")
        return 0

    hits = 0
    for r in rows:
        if (r.get("role") or "") != "user":
            continue
        if match(str(r.get("content") or "")):
            hits += 1
        else:
            break
    return hits


def get_recent_messages(session_id, limit=10):
    """某个会话最近的对话（时间正序），用于把上下文上传给 Dify。

    返回 [{"role", "content", "create_time"}, ...]。
    """
    if not session_id:
        return []
    try:
        rows = list(_coll(AFTER_SALES_COLLECTION)
                    .find({"session_id": str(session_id)},
                          {"_id": 0, "role": 1, "content": 1, "create_time": 1,
                           "read_user": 1, "read_agent": 1})
                    .sort([("_id", -1)]).limit(int(limit)))
    except Exception as e:
        print(f"⚠️ 读取对话历史失败：{_brief(e)}")
        return []
    rows.reverse()
    return rows