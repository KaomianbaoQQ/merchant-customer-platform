"""把历史聊天记录一次性迁进 MongoDB（售前 chat.json + 售后 cs.chat_log）。

运行（在项目根目录）：
    .venv/bin/python 工具/迁移聊天记录到Mongo.py            # 正式导入
    .venv/bin/python 工具/迁移聊天记录到Mongo.py --dry-run  # 只看条数，不写库

特点：
  · 幂等：售前按「商户 + 顾客」、售后按 MySQL 原 id 做 upsert，重复跑不会产生重复数据；
  · 只读源数据：不删除也不修改 chat.json 与 cs.chat_log；
  · 结束打印「源条数 vs Mongo 条数」对照，方便核对迁移结果。
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 公共 import chat_store, store
from 公共.db_config import MONGO_CONFIG, get_conn


def _presale_coll():
    return chat_store.get_client()[MONGO_CONFIG["database"]][chat_store.PRESALE_COLLECTION]


def _after_sales_coll():
    return chat_store.get_client()[MONGO_CONFIG["database"]][chat_store.AFTER_SALES_COLLECTION]


def _load_json_threads():
    """读旧的 chat.json（售前会话），返回 {key: thread}。"""
    path = store.CHAT_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 {path} 失败：{e}")
        return {}
    threads = data.get("threads") if isinstance(data, dict) else {}
    return threads if isinstance(threads, dict) else {}


def _load_mysql_rows():
    """读 cs.chat_log（售后对话），按 id 升序。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, session_id, user_id, user_name, role, content, create_time "
                        "FROM `chat_log` ORDER BY id")
            return list(cur.fetchall())
    finally:
        conn.close()


def migrate_presale(dry_run=False):
    """售前会话：chat.json → presale_threads（按 商户+顾客 upsert）。"""
    threads = _load_json_threads()
    src_msgs = sum(len(t.get("messages") or []) for t in threads.values())
    print("\n==== 售前会话（%s）====" % store.CHAT_FILE)
    print("源：%d 个会话 / %d 条消息" % (len(threads), src_msgs))

    coll = _presale_coll()
    if not dry_run:
        for thread in threads.values():
            merchant_id = str(thread.get("merchant_id") or "platform")
            user_id = str(thread.get("user_id") or "guest")
            doc = {k: v for k, v in thread.items() if k != "_id"}
            doc["merchant_id"] = merchant_id
            doc["user_id"] = user_id
            doc["key"] = doc.get("key") or ("%s::%s" % (merchant_id, user_id))
            doc.setdefault("messages", [])
            doc.setdefault("unread_merchant", 0)
            doc.setdefault("unread_user", 0)
            coll.update_one({"merchant_id": merchant_id, "user_id": user_id},
                            {"$set": doc}, upsert=True)

    got_threads = coll.count_documents({})
    got_msgs = sum(len(t.get("messages") or []) for t in coll.find({}, {"messages": 1}))
    print("Mongo：%d 个会话 / %d 条消息%s"
          % (got_threads, got_msgs, "（未写入，仅统计）" if dry_run else ""))
    return len(threads), src_msgs


def migrate_after_sales(dry_run=False):
    """售后对话：cs.chat_log → after_sales_messages（按 MySQL 原 id upsert）。"""
    print("\n==== 售后对话（MySQL cs.chat_log）====")
    try:
        rows = _load_mysql_rows()
    except Exception as e:
        print("⚠️ 读取 cs.chat_log 失败，跳过：%s" % e)
        return 0, 0
    sessions = {str(r.get("session_id") or "") for r in rows}
    print("源：%d 条消息 / %d 个会话" % (len(rows), len(sessions)))

    coll = _after_sales_coll()
    if not dry_run:
        for r in rows:
            create_time = r.get("create_time")
            if not isinstance(create_time, datetime):
                create_time = datetime.now()
            coll.update_one({"legacy_id": int(r.get("id"))},
                            {"$set": {
                                "legacy_id": int(r.get("id")),
                                "session_id": str(r.get("session_id") or ""),
                                "user_id": str(r.get("user_id") or ""),
                                "user_name": r.get("user_name"),
                                "role": r.get("role"),
                                "content": str(r.get("content") or ""),
                                "create_time": create_time,
                                "created_at": create_time.strftime("%Y-%m-%d %H:%M:%S"),
                            }}, upsert=True)

    got = coll.count_documents({})
    got_sessions = len(coll.distinct("session_id"))
    print("Mongo：%d 条消息 / %d 个会话%s"
          % (got, got_sessions, "（未写入，仅统计）" if dry_run else ""))
    return len(rows), len(sessions)


def main():
    dry_run = "--dry-run" in sys.argv
    print("==== 迁移聊天记录到 MongoDB（NoSQL）====")
    print("目标：%s / 库 %s" % (MONGO_CONFIG["uri"], MONGO_CONFIG["database"]))

    ok, msg = chat_store.ensure_indexes()
    if not ok:
        print("❌ %s\n请先启动 MongoDB（docker start testmall-mongo）后重试。" % msg)
        return
    print("✅ %s" % msg)
    if dry_run:
        print("（--dry-run：只看源数据条数，不写库）")

    migrate_presale(dry_run)
    migrate_after_sales(dry_run)

    print("\n==== 完成 ====")
    print("源数据（chat.json / cs.chat_log）保持原样，未删除也未修改。")
    if not dry_run:
        print("重复执行本脚本不会产生重复数据。")


if __name__ == "__main__":
    main()