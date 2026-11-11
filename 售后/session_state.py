"""售后会话的临时状态（进程内）。

只保存「一问一答之间」需要记住的短期状态：
    pending  客服刚问完「是哪一笔订单」，等顾客用序号 / 订单号回答时的候选列表
    human    该会话是否已经转人工客服

进程重启后这些状态会丢失，顾客重新说一次即可（不影响已落库的订单 / 售后单）。

对外接口：
    set_pending(session_id, state) / get_pending(session_id) / clear_pending(session_id)
    mark_human(session_id) / is_human(session_id) / clear_human(session_id)
"""
import threading

_lock = threading.Lock()
_pending = {}
_human = set()  # 已转人工的 session_id

def set_pending(session_id, state):
    """记住「等顾客选哪一笔订单」的上下文。"""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _pending[sid] = dict(state or {})

def get_pending(session_id):
    """取回待选择状态；没有返回 None。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    with _lock:
        state = _pending.get(sid)
        return dict(state) if state else None

def clear_pending(session_id):
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _pending.pop(sid, None)

def mark_human(session_id):
    """标记该会话已转人工。"""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _human.add(sid)

def is_human(session_id):
    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _lock:
        return sid in _human

def clear_human(session_id):
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        _human.discard(sid)
