"""一键清空全部业务数据（**保留行政区划 region**，它只读、不是业务数据）。

清空范围：
    MySQL cs   user / merchant / order / return_order / logistics / review /
               address_book / chat_log / operate_log
    MongoDB    presale_threads / after_sales_messages / user_behaviors
    文件       商户界面/products.json          商品
               商户界面/chat.json              旧聊天文件（已迁 Mongo）
               商户界面/platform_state.json    商家停用 / 禁言 / 商品下架 / 罚款
               商户界面/knowledge_map.json     商品 → Dify 知识库文档映射
               商品咨询/*.md                    商品资料（每个商品一份）
               商户界面/images/*                商家上传的商品图片

**不动**：cs.region（省市区参考数据）、售后/admin.json（平台管理员账号）、
Dify 知识库里的文档（如需一并删除，请在 Dify 里手动清理）。

用法：
    .venv/bin/python 工具/清空数据.py          # 只预览将清空的内容，不动数据
    .venv/bin/python 工具/清空数据.py --yes    # 真正执行（不可恢复）
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 公共.db_config import get_conn, MONGO_CONFIG
from 公共.platform_state import DEFAULT as PLATFORM_STATE_DEFAULT, STATE_FILE

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERCHANT_DIR = os.path.join(ROOT_DIR, "商户界面")
PRODUCTS_FILE = os.path.join(MERCHANT_DIR, "products.json")
CHAT_FILE = os.path.join(MERCHANT_DIR, "chat.json")
KNOWLEDGE_MAP_FILE = os.path.join(MERCHANT_DIR, "knowledge_map.json")
IMAGE_DIR = os.path.join(MERCHANT_DIR, "images")
PRODUCT_DOC_DIR = os.path.join(ROOT_DIR, "商品咨询")

# MySQL 里要清空的业务表（region 是行政区划参考数据，必须保留）
MYSQL_TABLES = [
    # 子表 / 关联表在前，避免外键约束阻止清空
    "stock_reservation_item", "stock_reservation", "sku_stock",
    "cart", "return_order", "refund_ledger", "logistics",
    "order", "address_book", "review", "chat_log", "operate_log",
    "email_code", "user", "merchant",
]
MONGO_COLLECTIONS = ["presale_threads", "after_sales_messages", "user_behaviors"]

def _mysql_preview():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            existing = {list(r.values())[0] for r in cur.fetchall()}
            counts = {}
            for t in MYSQL_TABLES:
                if t not in existing:
                    continue
                cur.execute(f"SELECT COUNT(*) n FROM `{t}`")
                counts[t] = cur.fetchone()["n"]
        return counts
    finally:
        conn.close()

def _mongo_preview():
    from 公共.chat_store import get_client
    db = get_client()[MONGO_CONFIG["database"]]
    return {c: db[c].count_documents({}) for c in MONGO_COLLECTIONS}

def _file_preview():
    items = []
    for path, label in ((PRODUCTS_FILE, "商品"), (CHAT_FILE, "旧聊天文件"),
                        (KNOWLEDGE_MAP_FILE, "知识库映射"), (STATE_FILE, "平台管控状态")):
        if os.path.isfile(path):
            items.append((path, label, "有数据"))
    if os.path.isdir(PRODUCT_DOC_DIR):
        docs = glob.glob(os.path.join(PRODUCT_DOC_DIR, "*.md"))
        if docs:
            items.append((PRODUCT_DOC_DIR + "/*.md", "商品资料", f"{len(docs)} 个文件"))
    if os.path.isdir(IMAGE_DIR):
        imgs = [f for f in os.listdir(IMAGE_DIR) if not f.startswith(".")]
        if imgs:
            items.append((IMAGE_DIR + "/*", "商品图片", f"{len(imgs)} 个文件"))
    return items

def _write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)

def clear_mysql():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            existing = {list(r.values())[0] for r in cur.fetchall()}
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t in MYSQL_TABLES:
                if t not in existing:
                    continue
                try:
                    cur.execute(f"TRUNCATE TABLE `{t}`")   # 顺带重置自增 ID
                except Exception:
                    # TRUNCATE 被拒（含外键引用）时退化为 DELETE + 重置自增
                    cur.execute(f"DELETE FROM `{t}`")
                    cur.execute(f"ALTER TABLE `{t}` AUTO_INCREMENT = 1")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()

def clear_mongo():
    from 公共.chat_store import get_client
    db = get_client()[MONGO_CONFIG["database"]]
    for c in MONGO_COLLECTIONS:
        db[c].delete_many({})

def clear_files():
    _write_json(PRODUCTS_FILE, [])
    _write_json(CHAT_FILE, [])
    _write_json(KNOWLEDGE_MAP_FILE, {})
    _write_json(STATE_FILE, PLATFORM_STATE_DEFAULT)

    if os.path.isdir(PRODUCT_DOC_DIR):
        for path in glob.glob(os.path.join(PRODUCT_DOC_DIR, "*.md")):
            os.remove(path)

    if os.path.isdir(IMAGE_DIR):
        for name in os.listdir(IMAGE_DIR):
            if name.startswith("."):
                continue
            full = os.path.join(IMAGE_DIR, name)
            if os.path.isfile(full):
                os.remove(full)

def main():
    execute = "--yes" in sys.argv
    print("=" * 60)
    print("将清空的 MySQL 业务表（region 行政区划保留）：")
    try:
        for t, n in _mysql_preview().items():
            print(f"    {t:<16} {n} 行")
    except Exception as e:
        print(f"    MySQL 读取失败：{e}")

    print("将清空的 MongoDB 集合：")
    try:
        for c, n in _mongo_preview().items():
            print(f"    {c:<22} {n} 条")
    except Exception as e:
        print(f"    MongoDB 读取失败（不影响的其它步骤仍可执行）：{e}")

    print("将清空的文件：")
    for path, label, info in _file_preview():
        print(f"    {label:<8} {info:<10} {path}")
    print("=" * 60)

    if not execute:
        print("当前是【预览模式】，没有改动任何数据。")
        print("确认无误后执行：.venv/bin/python 工具/清空数据.py --yes")
        return

    clear_mysql()
    print("✅ MySQL 业务表已清空（region 保留）")
    try:
        clear_mongo()
        print("✅ MongoDB 集合已清空")
    except Exception as e:
        print(f"⚠️ MongoDB 清空失败（请确认容器 testmall-mongo 已启动）：{e}")
    clear_files()
    print("✅ 商品 / 聊天文件 / 平台状态 / 知识库映射 / 商品资料 / 上传图片已清空")
    print("完成。顾客与商户账号也一并清掉了，需要登录演示账号请执行：")
    print("  .venv/bin/python 工具/创建演示账号.py")

if __name__ == "__main__":
    main()