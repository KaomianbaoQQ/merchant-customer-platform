"""数据库连接配置。所有 db_* 模块共用，避免到处复制粘贴密码。

口令等敏感项统一放在 config.json 的 database / mongo 段（该文件已被 .gitignore 排除），
也可以用环境变量覆盖：KB_DB_HOST / KB_DB_PORT / KB_DB_USER / KB_DB_PASSWORD / KB_MONGO_URI。
首次配置请运行：.venv/bin/python 工具/初始化配置.py

聊天记录的 NoSQL 存储（MongoDB）。容器由「一键启动全部.command」负责拉起：
    docker run -d --name testmall-mongo -p 127.0.0.1:27017:27017 \
      -v testmall_mongo_data:/data/db --restart unless-stopped mongo:7
"""
import pymysql

from 公共.app_config import db_config as _db_config, mongo_config as _mongo_config

# 保持模块级常量的老用法（其它模块大量 import 这两个名字）
DB_CONFIG = _db_config()
MONGO_CONFIG = _mongo_config()


def get_conn():
    """获取数据库连接（查询结果以 dict 返回）"""
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
