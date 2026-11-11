"""物流状态：根据「发货时间 / 预计送达时间 / 当前时间」动态匹配。

状态共 5 档（与前端进度条一致）：
    0 待发货 / 1 已发货 / 2 运输中 / 3 派送中 / 4 已签收

运单号由商家填写、发货后才写入物流记录；没有发货时间的订单统一显示「待发货」。

规则（total = 预计送达 - 发货）：
    now >= 预计送达                         → 4 已签收
    剩余 <= min(1 小时, total * 25%)         → 3 派送中
    已用 < total * 15%                       → 1 已发货
    其余                                      → 2 运输中
"""
from datetime import datetime

STATUS_TEXTS = ["待发货", "已发货", "运输中", "派送中", "已签收"]

# 派送中窗口：距离预计送达不足 min(1 小时, 总时长 25%) 时进入派送中
DELIVERY_WINDOW_SECONDS = 3600
DELIVERY_WINDOW_RATIO = 0.25
SHIPPED_RATIO = 0.15


def parse_datetime(value):
    """把 datetime / 字符串统一解析成 datetime，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_datetime(dt):
    """统一输出 ISO 8601（带 T），方便浏览器 new Date() 解析。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt is not None else None


def compute_status(shipped_at, estimated_arrival, now=None):
    """根据发货时间 / 预计送达时间 / 当前时间，动态计算物流档位。
    返回 (step:int, text:str)。"""
    now = now or datetime.now()

    shipped_at = parse_datetime(shipped_at)
    estimated_arrival = parse_datetime(estimated_arrival)
    if shipped_at is None or estimated_arrival is None:
        return 0, STATUS_TEXTS[0]

    if now >= estimated_arrival:
        return 4, STATUS_TEXTS[4]

    total = (estimated_arrival - shipped_at).total_seconds()
    if total <= 0:
        return 4, STATUS_TEXTS[4]

    elapsed = (now - shipped_at).total_seconds()
    if elapsed < 0:
        # 刚下单时 MySQL 按秒取整可能让发货时间比 now 晚零点几秒，视为已发货
        elapsed = 0

    remaining = (estimated_arrival - now).total_seconds()
    delivery_window = min(DELIVERY_WINDOW_SECONDS, total * DELIVERY_WINDOW_RATIO)

    if remaining <= delivery_window:
        return 3, STATUS_TEXTS[3]
    if elapsed < total * SHIPPED_RATIO:
        return 1, STATUS_TEXTS[1]
    return 2, STATUS_TEXTS[2]
