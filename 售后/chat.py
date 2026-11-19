"""售后客服对话记录：已从 MySQL cs.chat_log 迁移到 MongoDB（NoSQL）。

实现见 `公共/chat_store.py`（集合 after_sales_messages，一条消息一个文档）；
本模块保留原有的函数名与签名，调用点（后端/routes/after_sales.py、售后/service.py、
售后/cli.py）都不用改。

保存失败不会影响主流程（只打印警告）。

注意：`cs.chat_log` 的表结构与历史数据都**保留不删**，只是不再写入；
历史数据由「工具/迁移聊天记录到Mongo.py」一次性导入 MongoDB。
"""
from 公共 import chat_store


def ensure_chat_table():
    """确保聊天记录集合与索引就绪。返回 (是否成功, 提示信息)。

    沿用原函数名（原来是建 cs.chat_log 表），启动自检 / 健康检查不用改。
    """
    return chat_store.ensure_indexes()


def new_session_id(user_id):
    """生成一次登录的会话ID，如 99-20260910203015"""
    return chat_store.new_session_id(user_id)


def save_message(session_id, user_id, user_name, role, content, images=None):
    """保存一条对话消息。content 为空则跳过；出错只打印警告，不抛异常。
    返回 True/False。
    """
    return chat_store.save_message(session_id, user_id, user_name, role, content, images=images)


def save_turn(session_id, user, user_input, reply):
    """保存一问一答（用户 + 客服两条）"""
    chat_store.save_turn(session_id, user, user_input, reply)


def count_recent_consecutive(session_id, match, scan=50):
    """从最近一条「顾客消息」往前数，连续满足 match(text) 的条数。

    客服回复不打断连续（只看 role=user 的消息），因此顾客连说 3 次「人工」
    就是 3 次；中间说了别的就会归零。供售后「连续 3 次人工转人工」判定使用。
    """
    return chat_store.count_recent_consecutive(session_id, match, scan)


def get_recent_messages(session_id, limit=10):
    """获取某个会话最近的对话（时间正序），用于把上下文上传给 Dify。
    返回 [{"role", "content", "create_time"}, ...]。
    """
    return chat_store.get_recent_messages(session_id, limit)


def mark_session_read(session_id):
    """顾客点进售后面板：把客服 / 机器人已发出的消息标记为「已读」。"""
    chat_store.mark_after_sales_read(session_id)