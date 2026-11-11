"""Kaomianbao 后端应用：Flask 工厂 + 启动。

    create_app()   创建 Flask 应用并注册蓝图 / CORS
    bootstrap()    启动前自检并建表（cs.order / cs.logistics / cs.chat_log / SKU 库存）
    run_server()   启动 HTTP 服务 + 库存预占超时清理线程
"""
from flask import Flask

from 售后.order import ensure_tables
from 售后.address import ensure_address_table
from 售后.chat import ensure_chat_table
from 售后.returns import ensure_return_table
from 售后.review import ensure_tables as ensure_review_tables
from 物流.repository import ensure_logistics
from 公共.accounts import ensure_merchant_table
from 公共.behavior import ensure_indexes as ensure_behavior_indexes
from 公共 import inventory
from .routes import register_routes
from .public_access import guard


def cors_headers(resp):
    """统一的跨域响应头。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


def create_app():
    """创建并配置 Flask 应用。"""
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.after_request(cors_headers)
    app.before_request(guard)
    register_routes(app)
    return app


def bootstrap():
    """启动前确保相关表 / 字段存在。"""
    ensure_tables()
    ensure_logistics()
    ensure_chat_table()
    ensure_return_table()
    ensure_address_table()
    ensure_merchant_table()
    ensure_review_tables()
    ensure_behavior_indexes()
    inventory.ensure_tables()
    inventory.sync_all_products()


def run_server(host="0.0.0.0", port=8000):
    """启动 HTTP 服务。"""
    bootstrap()
    # 过期未支付的预占需要定时释放，否则库存会被永久锁住
    inventory.start_cleanup_worker()
    app = create_app()
    print(f"Kaomianbao 商城后端启动：http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
