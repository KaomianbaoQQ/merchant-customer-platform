# -*- coding: utf-8 -*-
"""商品知识库同步 —— 把商户上架 / 修改 / 删除的商品落盘为文件，并异步同步到 Dify 知识库。

核心目标：
  · 每次商户增 / 改 / 删商品，都会在「商品咨询」文件夹落盘保存一个商品文件（这是硬性动作，
    与 Dify 是否可用无关，Dify 挂掉也不影响文件保存）；
  · 知识库文档的内容「从该商品文件读取」，即知识库是从 商品咨询 文件夹上传的；
  · 同步在后台线程执行，请求立即返回，「同步」与「查找 / 商品管理」并行不互相阻塞；
  · 本地保存 product_id -> document_id 映射，后续修改 / 删除时能精确找到对应文档。

配置：config.json 的 `dify_knowledge` 字段：
    api_key            知识库 Dataset 的 API Key（dataset- / app- 开头）
    dataset_id         知识库 Dataset 的 ID
    base_url           Dify 接口地址，默认 http://localhost/v1
    map_file           product_id→document_id 映射文件路径
    product_dir        商品文件落盘目录，默认「商品咨询」
    indexing_technique 索引方式：economy（关键词，默认）/ high_quality（向量，需配置 embedding）

Dify Dataset 用法：
    create  POST   /datasets/{dataset}/document/create-by-text
    update  PUT    /datasets/{dataset}/documents/{document_id}/update-by-text
    delete  DELETE /datasets/{dataset}/documents/{document_id}
    metadata POST  /datasets/{dataset}/documents/metadata
            （给文档写 merchant_id / merchant_name，供检索节点按商家过滤，
              需先在 Dify 知识库建好这两个元数据字段）
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT_DIR, "config.json")

_PLACEHOLDER_RE = re.compile(r"app-|请填写|请填入|在此填入|在此填写", re.IGNORECASE)

# 后台执行 Dify 同步的线程池（最大同时 2 个任务，避免打爆 Dify 并发）
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dify-kb")
_mapping_lock = threading.RLock()


# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return cfg.get("dify_knowledge") or {}


def get_knowledge_settings():
    """读取全部知识库相关配置，返回 dict。"""
    cfg = _load_config()
    base_url = str(cfg.get("base_url") or "http://localhost/v1").rstrip("/")
    api_key = str(cfg.get("api_key") or "").strip()
    dataset_id = str(cfg.get("dataset_id") or "").strip()

    map_file = str(cfg.get("map_file") or "商户界面/knowledge_map.json").strip()
    if not map_file.startswith("/"):
        map_file = os.path.join(ROOT_DIR, map_file)

    product_dir = str(cfg.get("product_dir") or "商品咨询").strip()
    if not product_dir.startswith("/"):
        product_dir = os.path.join(ROOT_DIR, product_dir)

    technique = str(cfg.get("indexing_technique") or "economy").strip()
    if technique not in ("high_quality", "economy"):
        technique = "economy"

    valid = bool(api_key and dataset_id and not _PLACEHOLDER_RE.search(api_key))
    return {
        "base_url": base_url,
        "api_key": api_key,
        "dataset_id": dataset_id,
        "map_file": map_file,
        "product_dir": product_dir,
        "indexing_technique": technique,
        "valid": valid,
    }


def get_dify_config():
    """兼容旧调用：返回 (base_url, api_key, dataset_id, map_file, valid)。"""
    s = get_knowledge_settings()
    return (s["base_url"], s["api_key"], s["dataset_id"], s["map_file"], s["valid"])


# ------------------------------------------------------------
# 映射持久化（product_id → document_id）
# ------------------------------------------------------------
def _load_map(map_file):
    if not os.path.exists(map_file):
        return {}
    try:
        with open(map_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_map(map_file, data):
    os.makedirs(os.path.dirname(map_file) or ".", exist_ok=True)
    tmp = map_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, map_file)


def _get_document_id(map_file, product_id):
    return (_load_map(map_file).get(str(product_id)) or {}).get("document_id")


def _set_document(map_file, product_id, document_id):
    with _mapping_lock:
        data = _load_map(map_file)
        record = data.get(str(product_id)) or {}
        record["document_id"] = document_id
        record["document_name"] = _document_name(product_id)
        record["synced_at"] = _now()
        data[str(product_id)] = record
        _save_map(map_file, data)


def _drop_document(map_file, product_id):
    with _mapping_lock:
        data = _load_map(map_file)
        if str(product_id) in data:
            del data[str(product_id)]
            _save_map(map_file, data)


# ------------------------------------------------------------
# 商品文件（落盘到 商品咨询 文件夹）
# ------------------------------------------------------------
def _safe_filename(product_id):
    """把商品 id 转成安全的文件名（去路径分隔符 / 空白）。"""
    name = re.sub(r'[/\\:*?"<>|]', "-", str(product_id or "")).strip()
    return (name or "product")


def product_file_path(product_id):
    """返回某商品在 商品咨询 文件夹中的文件绝对路径。"""
    return os.path.join(get_knowledge_settings()["product_dir"],
                        _safe_filename(product_id) + ".md")


def build_product_text(product):
    """把商品所有信息拼成一段可供知识库检索的纯文本。"""
    product = product or {}
    name = str(product.get("name") or "").strip() or "未命名商品"
    tag = str(product.get("tag") or "").strip()
    price = product.get("price")
    desc = str(product.get("desc") or "").strip()
    specs = product.get("specs") or []
    owner = str(product.get("owner_name") or "").strip()

    lines = [f"商品名称：{name}"]
    if tag:
        lines.append(f"所属类目：{tag}")
    if price != "" and price is not None:
        lines.append(f"价格：¥{price}")
    if owner:
        lines.append(f"所属店铺：{owner}")
    if desc:
        lines.append(f"商品简介：{desc}")

    clean_specs = []
    for kv in specs:
        if not isinstance(kv, (list, dict)):
            continue
        if isinstance(kv, dict):
            k, v = kv.get("k"), kv.get("v")
        else:
            k, v = (kv + [None, None])[:2]
        if k and v:
            clean_specs.append(f"- {k}：{v}")
    if clean_specs:
        lines.append("规格参数：\n" + "\n".join(clean_specs))

    return "\n".join(lines)


def write_product_file(product):
    """把商品落盘为 md 文件（硬性动作），返回文件绝对路径；失败返回 None。"""
    product = product or {}
    product_id = _product_id_of(product)
    if not product_id:
        return None
    path = product_file_path(product_id)
    text = build_product_text(product)
    content = (
        f"# {str(product.get('name') or '').strip() or '未命名商品'}\n\n"
        f"- 商品ID：{product_id}\n"
        f"- 更新时间：{product.get('updated_at') or _now()}\n\n"
        f"{text}\n"
    )
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        return path
    except Exception as e:
        print(f"[知识库] 商品文件写入失败：{e}")
        return None


def remove_product_file(product_id):
    """删除商品在 商品咨询 文件夹的文件（硬性动作），返回是否删除。"""
    path = product_file_path(product_id)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception as e:
        print(f"[知识库] 商品文件删除失败：{e}")
    return False


def read_product_file(product_id):
    """读取商品文件的文本内容（用于上传知识库）。文件不存在返回 None。"""
    path = product_file_path(product_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        print(f"[知识库] 读取商品文件失败：{e}")
    return None


def _document_name(product_id):
    return f"product-{product_id}"


def _product_id_of(product):
    return str((product or {}).get("id") or "").strip()


# ------------------------------------------------------------
# Dify Dataset 调用
# ------------------------------------------------------------
def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _doc_create(base_url, api_key, dataset_id, product_id, text, technique):
    """新增文档，返回 (ok, document_id, message)。"""
    url = f"{base_url}/datasets/{dataset_id}/document/create-by-text"
    body = {
        "name": _document_name(product_id),
        "text": text,
        "indexing_technique": technique,
        "process_rule": {"mode": "automatic"},
        "doc_form": "text_model",
    }
    resp = requests.post(url, headers=_headers(api_key), json=body, timeout=30)
    resp.raise_for_status()
    payload = resp.json() or {}
    # 新版 Dify：document_id 在 "document" 对象里；旧版可能返回 "data" 数组
    doc_id = None
    if isinstance(payload.get("document"), dict):
        doc_id = payload["document"].get("id")
    if not doc_id:
        for item in payload.get("data") or [] if isinstance(payload.get("data"), list) else []:
            if isinstance(item, dict) and item.get("id"):
                doc_id = item.get("id")
                break
    if not doc_id and isinstance(payload.get("data"), dict):
        doc_id = payload["data"].get("id")
    if not doc_id:
        return False, None, "Dify 未返回 document_id"
    return True, doc_id, "created"


def _doc_update(base_url, api_key, dataset_id, document_id, product_id, text):
    """更新文档内容。"""
    url = f"{base_url}/datasets/{dataset_id}/documents/{document_id}/update-by-text"
    resp = requests.post(
        url, headers=_headers(api_key),
        json={"name": _document_name(product_id), "text": text},
        timeout=30,
    )
    resp.raise_for_status()
    return True, "updated"


def _doc_delete(base_url, api_key, dataset_id, document_id):
    """删除文档。"""
    url = f"{base_url}/datasets/{dataset_id}/documents/{document_id}"
    resp = requests.delete(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return True, "deleted"


def _doc_set_metadata(base_url, api_key, dataset_id, document_id, product):
    """给知识库文档写「商家」元数据（merchant_id / merchant_name）。

    Dify 的检索节点可用它做元数据过滤，从而实现「A 商家商品页的客服不会召回
    B 商家资料」。前提：先在 Dify 知识库的元数据里建好这两个字段（字符串）。
    元数据失败只告警，不影响商品文件的落盘与正文同步。
    """
    merchant_id = str((product or {}).get("owner_id") or "").strip()
    merchant_name = str((product or {}).get("owner_name") or "").strip()
    if not document_id or not (merchant_id or merchant_name):
        return False, "无商家信息，跳过元数据"
    items = []
    if merchant_id:
        items.append({"name": "merchant_id", "value": merchant_id})
    if merchant_name:
        items.append({"name": "merchant_name", "value": merchant_name})

    url = f"{base_url}/datasets/{dataset_id}/documents/metadata"
    body = {"operation_data": [{"document_id": document_id, "metadata_list": items}]}
    try:
        resp = requests.post(url, headers=_headers(api_key), json=body, timeout=30)
        resp.raise_for_status()
        return True, "metadata_set"
    except requests.exceptions.HTTPError as e:
        detail = ""
        try:
            detail = (e.response.text or "")[:200]
        except Exception:
            pass
        print(f"[知识库] 元数据写入失败：{e} {detail}"
              "（请确认 Dify 知识库里已创建 merchant_id / merchant_name 元数据字段）")
        return False, "metadata_failed"
    except Exception as e:
        print(f"[知识库] 元数据写入失败：{e}")
        return False, "metadata_failed"


# ------------------------------------------------------------
# 后台同步入口
# ------------------------------------------------------------
def _sync_doc(action, product):
    """单次同步：先落盘商品文件（无条件），再视配置同步到 Dify。"""
    s = get_knowledge_settings()
    product_id = _product_id_of(product)
    if not product_id:
        return {"ok": False, "skipped": True, "message": "缺少商品 id"}

    # ---- 无论 Dify 是否可用，先落盘 / 删除商品文件 ----
    if action == "delete":
        remove_product_file(product_id)
    else:
        write_product_file(product)

    # ---- Dify 未配置有效凭据时到此为止，不影响商品文件保存 ----
    if not s["valid"]:
        print("[知识库] 未配置有效的 Dify 知识库凭据，仅保存商品文件")
        return {"ok": False, "skipped": True, "message": "Dify 知识库未配置，仅保存商品文件"}

    result = {"ok": False, "skipped": False, "action": action,
              "product_id": product_id, "document_id": None, "message": ""}
    map_file = s["map_file"]
    try:
        if action == "delete":
            doc_id = _get_document_id(map_file, product_id)
            if doc_id:
                ok, msg = _doc_delete(s["base_url"], s["api_key"], s["dataset_id"], doc_id)
                if ok:
                    _drop_document(map_file, product_id)
                result.update(ok=ok, document_id=doc_id, message=msg)
            else:
                result.update(ok=True, message="本地无映射，跳过删除")
            return result

        # create / update
        text = read_product_file(product_id) or build_product_text(product)
        doc_id = _get_document_id(map_file, product_id)
        if action in ("update", "create") and doc_id:
            ok, msg = _doc_update(s["base_url"], s["api_key"], s["dataset_id"],
                                  doc_id, product_id, text)
            if ok:
                # 正文更新后重写商家元数据（商家可能改名 / 商品换店主）
                _doc_set_metadata(s["base_url"], s["api_key"], s["dataset_id"],
                                  doc_id, product)
            result.update(ok=ok, document_id=doc_id, message=msg)
            return result

        # 缺少映射或显式 create → 新增文档并落映射
        ok, doc_id, msg = _doc_create(s["base_url"], s["api_key"], s["dataset_id"],
                                      product_id, text, s["indexing_technique"])
        if ok and doc_id:
            _set_document(map_file, product_id, doc_id)
            # 建完文档立刻打上商家元数据，供检索节点按商家过滤
            _doc_set_metadata(s["base_url"], s["api_key"], s["dataset_id"], doc_id, product)
        result.update(ok=ok, document_id=doc_id, message=msg)
        return result
    except requests.exceptions.ConnectionError:
        result.update(message="连接 Dify 失败，请确认服务已启动")
        return result
    except Exception as e:  # 网络 / Dify 返回异常都不能影响商户侧
        result.update(message=f"同步异常：{e}")
        return result


def async_sync_knowledge(action, product=None):
    """提交一个同步任务到后台线程，立即返回，不阻塞调用方。

    该任务一定会把商品文件落盘到 商品咨询 文件夹；Dify 可用时再同步到知识库。
    """
    action = str(action or "").strip().lower()
    if action not in ("create", "update", "delete"):
        return {"ok": False, "message": f"未知操作：{action}"}
    try:
        _executor.submit(_run_sync_logged, action, product or {})
        return {"ok": True, "pending": True, "action": action,
                "message": "已提交后台同步"}
    except Exception as e:
        return {"ok": False, "message": f"提交后台任务失败：{e}"}


def _run_sync_logged(action, product):
    """后台执行同步并打印结果，避免失败被静默吞掉（商品文件仍无条件落盘）。"""
    try:
        r = _sync_doc(action, product)
        pid = r.get("product_id") or _product_id_of(product) or "?"
        if r.get("ok"):
            print(f"[知识库] {action} 同步成功：{pid} -> {r.get('message')}")
        elif r.get("skipped"):
            print(f"[知识库] {action} 已跳过：{pid} -> {r.get('message')}")
        else:
            print(f"[知识库] {action} 同步失败：{pid} -> {r.get('message')}")
    except Exception as e:
        print(f"[知识库] {action} 后台同步异常：{e}")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")