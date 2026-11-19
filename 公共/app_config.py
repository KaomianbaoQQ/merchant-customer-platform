"""统一读取 config.json —— 所有密钥（邮箱授权码 / 各类 API Key / 数据库口令）只放在
配置文件或环境变量里，代码中不出现明文。

用法：
    from 公共.app_config import load, get, dify_workflow, db_config

    get("smtp.user")                       # 按点号路径取值，缺省返回 None
    dify_workflow("order_locate")          # -> {"api_key": ..., "base_url": ...}
    db_config()                            # -> MySQL 连接参数 dict

取值优先级：环境变量 > config.json > 内置默认值（仅限非敏感项，如 base_url）。
环境变量名见下面 ENV_MAP。
"""
import json
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
EXAMPLE_PATH = os.path.join(ROOT_DIR, "config.example.json")

# 环境变量覆盖（可选项，方便部署时不改文件）
ENV_MAP = {
    "database.password": "KB_DB_PASSWORD",
    "database.user": "KB_DB_USER",
    "database.host": "KB_DB_HOST",
    "database.port": "KB_DB_PORT",
    "mongo.uri": "KB_MONGO_URI",
    "express_tracking.api_key": "KB_EXPRESS_API_KEY",
    "dify_knowledge.api_key": "KB_DIFY_DATASET_KEY",
    "dify.workflows.order_locate.api_key": "KB_DIFY_ORDER_LOCATE_KEY",
    "dify.workflows.after_sales.api_key": "KB_DIFY_AFTER_SALES_KEY",
    "dify.workflows.consult.api_key": "KB_DIFY_CONSULT_KEY",
    "smtp.user": "KB_SMTP_USER",
    "smtp.password": "KB_SMTP_PASSWORD",
    "smtp.host": "KB_SMTP_HOST",
    "smtp.port": "KB_SMTP_PORT",
    "smtp.ssl": "KB_SMTP_SSL",
    "smtp.from_name": "KB_SMTP_FROM_NAME",
}

# Dify 工作流的内置默认地址（不含密钥，公开也无风险）
DEFAULT_DIFY_BASE_URL = "http://localhost/v1"

_cache = None


def load(force=False):
    """读取 config.json；不存在时返回空 dict（不抛异常，便于首次运行提示用户配置）。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        data = {}
    except Exception as e:
        print(f"⚠️ config.json 读取失败（已按空配置继续）：{e}")
        data = {}
    _cache = data if isinstance(data, dict) else {}
    return _cache


def get(path, default=None):
    """按点号路径取值，例如 get("smtp.user")。环境变量优先。"""
    env = ENV_MAP.get(path)
    if env:
        val = os.environ.get(env)
        if val not in (None, ""):
            return val
    cur = load()
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def dify_workflow(name, default_base_url=DEFAULT_DIFY_BASE_URL):
    """读取某个 Dify 工作流的 Key 与地址。

    config.json 结构：
        "dify": {
          "workflows": {
            "order_locate": {"api_key": "app-xxx"},
            "after_sales":  {"api_key": "app-xxx"},
            "consult":      {"api_key": "app-xxx"}
          }
        }
    地址可写在工作流内，也可写在 dify.base_url 作为公共默认。
    """
    key = get(f"dify.workflows.{name}.api_key", "") or ""
    base = (get(f"dify.workflows.{name}.base_url", "")
            or get("dify.base_url", "")
            or default_base_url)
    return {"api_key": str(key).strip(), "base_url": str(base).strip().rstrip("/")}


def db_config():
    """MySQL 连接参数。敏感项只在 config.json 的 database 段里。"""
    return {
        "host": get("database.host", "127.0.0.1"),
        "port": int(get("database.port", 3306) or 3306),
        "user": get("database.user", "root"),
        "password": str(get("database.password", "") or ""),
        "database": get("database.name", "cs"),
        "charset": get("database.charset", "utf8mb4"),
    }


def mongo_config():
    """MongoDB 连接参数。"""
    return {
        "uri": get("mongo.uri", "mongodb://127.0.0.1:27017"),
        "database": get("mongo.database", "testmall_chat"),
    }


def missing_keys():
    """返回尚未填写的关键配置项（用于启动自检 / 配置脚本提示）。"""
    checks = [
        ("database.password", "数据库密码"),
        ("express_tracking.api_key", "快递查询 API Key"),
        ("dify_knowledge.api_key", "Dify 知识库 Key"),
        ("dify.workflows.order_locate.api_key", "Dify「订单定位」工作流 Key"),
        ("dify.workflows.after_sales.api_key", "Dify「售后客服」工作流 Key"),
        ("dify.workflows.consult.api_key", "Dify「商品咨询」工作流 Key"),
        ("smtp.user", "SMTP 发件邮箱"),
        ("smtp.password", "SMTP 授权码"),
    ]
    out = []
    for path, label in checks:
        val = get(path, "")
        if not str(val or "").strip() or str(val).startswith("在此填入"):
            out.append((path, label))
    return out
