"""共享数据存储：商品、图片（JSON 文件）；商户 ↔ 顾客会话已迁到 MongoDB。

商户后台（商户界面/后端服务 server.py）与客户后端（后端 Flask）共用本模块，
从而保证「商家上架商品 → 客户前端立即可见」以及「顾客发消息 → 商家可回复」。

商品 / 图片仍存 JSON 文件：所有读写都带进程内线程锁 + 跨进程文件锁，避免两个服务
同时写坏 JSON。聊天记录改由 公共/chat_store.py 存 MongoDB，本模块只做同名转发。

商品只保存 SKU 定义（sku_id / name / price）；库存数量由 公共/inventory.py
单点维护在 MySQL，避免 JSON 与数据库双写不一致。
"""
import fcntl
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager

# 聊天记录（售前会话）转发到 MongoDB 实现，调用点保持不变
from 公共.chat_store import (  # noqa: F401
    append_message,
    get_thread,
    list_conversations,
    mark_human_ack,
    mark_needs_human,
    mark_read,
    mark_return_intent,
    clear_return_intent,
    search_threads,
    set_thread_meta,
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERCHANT_DIR = os.path.join(ROOT_DIR, "商户界面")
DATA_FILE = os.path.join(MERCHANT_DIR, "products.json")
CHAT_FILE = os.path.join(MERCHANT_DIR, "chat.json")
UPLOAD_IMAGE_DIR = os.path.join(MERCHANT_DIR, "images")
LEGACY_IMAGE_DIR = os.path.join(ROOT_DIR, "商品咨询", "商品图片")
# 新上传的图片优先，历史图片目录兜底
IMAGE_DIRS = (UPLOAD_IMAGE_DIR, LEGACY_IMAGE_DIR)

_thread_lock = threading.RLock()
_ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 底层读写
@contextmanager
def _locked():
    """进程内线程锁 + 跨进程文件锁。"""
    with _thread_lock:
        os.makedirs(MERCHANT_DIR, exist_ok=True)
        lock_path = os.path.join(MERCHANT_DIR, ".store.lock")
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def new_product_id(name):
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]+", "-", str(name or "")).strip("-")
    return (slug or "product") + "-" + time.strftime("%Y%m%d%H%M%S")

def normalize_product(body, existing=None):
    """把任意商户提交的数据整理成统一商品结构。

    字段：id / name / tag（自由类目）/ price / desc / images（多图）/
          img（首图，兼容旧数据）/ specs（[[参数名, 参数值], ...]）/
          skus（[{sku_id, name, price}, ...]，可售规格；库存数不落盘）
    """
    body = body or {}
    src = dict(existing or {})
    src.update(body)

    name = str(src.get("name") or "").strip()
    images = src.get("images")
    if not isinstance(images, list):
        images = []
    images = [str(x).strip() for x in images if str(x or "").strip()]
    # 兼容只传单张 img 的旧格式
    legacy_img = str(src.get("img") or "").strip()
    if legacy_img and legacy_img not in images:
        images.insert(0, legacy_img)

    specs = src.get("specs")
    if not isinstance(specs, list):
        specs = []
    clean_specs = []
    for kv in specs:
        if isinstance(kv, (list, tuple)) and len(kv) >= 2:
            k, v = str(kv[0] or "").strip(), str(kv[1] or "").strip()
        elif isinstance(kv, dict):
            k, v = str(kv.get("k") or "").strip(), str(kv.get("v") or "").strip()
        else:
            continue
        if k or v:
            clean_specs.append([k, v])

    try:
        price = float(src.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price.is_integer():
        price = int(price)

    product = {
        "id": str(src.get("id") or "").strip(),
        "name": name,
        "tag": str(src.get("tag") or "未分类").strip() or "未分类",
        "price": price,
        "desc": str(src.get("desc") or "").strip(),
        "images": images,
        "img": images[0] if images else "",
        "specs": clean_specs,
        "owner_id": str(src.get("owner_id") or "").strip(),
        "owner_name": str(src.get("owner_name") or "").strip(),
        "views": _to_int(src.get("views")),
        "updated_at": _now(),
    }
    if not product["id"]:
        product["id"] = new_product_id(name)
    if existing and existing.get("created_at"):
        product["created_at"] = existing["created_at"]
    else:
        product["created_at"] = src.get("created_at") or _now()

    # 一律剥离，库存由 公共/inventory.py 在 MySQL 单点维护。
    from 公共 import inventory
    product["skus"] = [
        {"sku_id": d["sku_id"], "name": d["name"], "price": d["price"]}
        for d in inventory.sku_definitions({
            "id": product["id"], "price": price, "skus": src.get("skus"),
        })
    ]
    return product

def list_products(owner_id=None, include_offline=False):
    """商品列表。owner_id 为空返回全部（客户前端）；否则只返回该商户的商品。

    被平台下架的商品默认不返回（顾客看不到、也买不到）；
    商家自己的后台与平台后台传 include_offline=True 时仍可看到（带 offline 标记）。
    """
    with _locked():
        data = _load(DATA_FILE, [])
    if not isinstance(data, list):
        return []
    if owner_id not in (None, ""):
        owner_id = str(owner_id)
        data = [p for p in data if str(p.get("owner_id") or "") == owner_id]
    elif not include_offline:
        from 公共.platform_state import offline_product_ids
        offline = offline_product_ids()
        if offline:
            data = [p for p in data if str(p.get("id")) not in offline]
    return data

def get_product(pid, include_offline=False):
    """按 ID 取商品。include_offline=True 时可取到被平台下架的商品（商家/平台用）。"""
    pid = str(pid or "")
    for p in list_products(include_offline=include_offline):
        if p.get("id") == pid:
            return p
    return None

def create_product(body):
    product = normalize_product(body)
    with _locked():
        data = _load(DATA_FILE, [])
        if not isinstance(data, list):
            data = []
        data.append(product)
        _save(DATA_FILE, data)
    return product

def update_product(pid, body):
    pid = str(pid or "")
    with _locked():
        data = _load(DATA_FILE, [])
        if not isinstance(data, list):
            data = []
        for i, p in enumerate(data):
            if p.get("id") == pid:
                merged = normalize_product(body, existing=p)
                merged["id"] = pid
                # 商品归属不允许被改写
                merged["owner_id"] = p.get("owner_id", "")
                merged["owner_name"] = p.get("owner_name", "")
                data[i] = merged
                _save(DATA_FILE, data)
                return merged
    return None

def delete_product(pid):
    pid = str(pid or "")
    with _locked():
        data = _load(DATA_FILE, [])
        if not isinstance(data, list):
            data = []
        remain = [p for p in data if p.get("id") != pid]
        if len(remain) == len(data):
            return False
        _save(DATA_FILE, remain)
    return True

def list_categories():
    """从现有商品中提取类目（去重、保序）。"""
    cats = []
    for p in list_products():
        tag = str(p.get("tag") or "").strip()
        if tag and tag not in cats:
            cats.append(tag)
    return cats

def increment_views(pid):
    """商品浏览量 +1，返回新的浏览量；商品不存在返回 None。"""
    pid = str(pid or "")
    if not pid:
        return None
    with _locked():
        data = _load(DATA_FILE, [])
        if not isinstance(data, list):
            return None
        for p in data:
            if str(p.get("id")) == pid:
                p["views"] = _to_int(p.get("views")) + 1
                _save(DATA_FILE, data)
                return p["views"]
    return None

def resolve_image(filename):
    """返回图片的绝对路径；找不到返回 None。"""
    name = os.path.basename(str(filename or ""))
    if not name:
        return None
    for d in IMAGE_DIRS:
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return None

def save_image(original_name, data):
    ext = os.path.splitext(str(original_name or ""))[1].lower()
    if ext not in _ALLOWED_EXT:
        ext = ".jpg"
    os.makedirs(UPLOAD_IMAGE_DIR, exist_ok=True)
    stored = "img_%s%s" % (uuid.uuid4().hex[:12], ext)
    with open(os.path.join(UPLOAD_IMAGE_DIR, stored), "wb") as f:
        f.write(data)
    return stored

# 商户 ↔ 顾客 会话
