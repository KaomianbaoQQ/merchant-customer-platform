"""物流模块 —— cs.logistics 物流表 + cs.order 物流字段读写。

职责：
    1. cs.order 表补充物流字段（收货地址 address / 运单号 waybill）
    2. 新建 cs.logistics 物流信息表
    3. 商家在「订单管理」中填写 7 位快递单号并发货，写入发货时间与预计送达时间
    4. 查询时调用 物流/status.py 动态匹配物流状态

说明：运单号不再由系统自动生成，必须由商家填写。
"""
import threading
from datetime import datetime, timedelta

from 公共.db_config import get_conn
from .waybill import load_default_delay
from .status import compute_status, format_datetime

ORDER_TABLE = "`order`"
LOGISTICS_TABLE = "`logistics`"

CARRIER = "测试商城速运"

CREATE_LOGISTICS_SQL = """
CREATE TABLE IF NOT EXISTS `logistics` (
    `waybill`           VARCHAR(32)  NOT NULL                 COMMENT '运单号(7位)',
    `order_ids`         TEXT                                  COMMENT '关联订单ID，多个用逗号分隔',
    `user_id`           VARCHAR(64)  DEFAULT NULL             COMMENT '下单用户ID',
    `user_name`         VARCHAR(64)  DEFAULT NULL             COMMENT '下单用户姓名',
    `address`           VARCHAR(255) DEFAULT NULL             COMMENT '收货地址',
    `carrier`           VARCHAR(64)  DEFAULT '测试商城速运' COMMENT '物流公司',
    `shipped_at`        DATETIME     DEFAULT NULL             COMMENT '发货时间',
    `estimated_arrival` DATETIME     DEFAULT NULL             COMMENT '预计送达时间',
    `delivered_at`      DATETIME     DEFAULT NULL             COMMENT '实际签收时间',
    `create_time`       DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`       DATETIME     DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`waybill`),
    KEY `idx_user` (`user_id`),
    KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='物流信息表';
"""

def _column_exists(cur, table_name, column_name):
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table_name, column_name),
    )
    return cur.fetchone() is not None

_ensure_done = False
_ensure_lock = threading.Lock()

def ensure_logistics():
    """确保 cs.logistics 表存在，并给 cs.order 补齐 address / waybill 字段。

    建表 / 补字段属于 DDL，会加元数据锁，因此进程内只执行一次；
    后续调用直接返回，避免每个请求都跑 DDL 造成并发阻塞。
    """
    global _ensure_done
    if _ensure_done:
        return True, "cs.logistics / cs.order 物流字段已就绪"
    with _ensure_lock:
        if _ensure_done:
            return True, "cs.logistics / cs.order 物流字段已就绪"
        ok, msg = _ensure_logistics_once()
        if ok:
            _ensure_done = True
        return ok, msg

def _ensure_logistics_once():
    """真正执行建表 / 补字段（仅进程内首次调用时执行）。"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_LOGISTICS_SQL)

            # 老表可能没有「实际签收时间」，CREATE TABLE IF NOT EXISTS 不会补列；
            # 售后时间窗与物流查询结果都依赖它，缺列会让新增的 SELECT 直接报错。
            if not _column_exists(cur, "logistics", "delivered_at"):
                cur.execute(
                    "ALTER TABLE `logistics` "
                    "ADD COLUMN `delivered_at` DATETIME DEFAULT NULL COMMENT '实际签收时间' "
                    "AFTER `estimated_arrival`"
                )

            # cs.order 补字段（老表没有 address / waybill）
            if not _column_exists(cur, "order", "address"):
                cur.execute(
                    f"ALTER TABLE {ORDER_TABLE} "
                    "ADD COLUMN `address` VARCHAR(255) DEFAULT NULL COMMENT '收货地址' AFTER `role`"
                )
            if not _column_exists(cur, "order", "waybill"):
                cur.execute(
                    f"ALTER TABLE {ORDER_TABLE} "
                    "ADD COLUMN `waybill` VARCHAR(32) DEFAULT NULL COMMENT '运单号' AFTER `address`"
                )
                cur.execute(f"ALTER TABLE {ORDER_TABLE} ADD KEY `idx_waybill` (`waybill`)")
        conn.commit()
        return True, "cs.logistics / cs.order 物流字段已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"物流表初始化失败：{e}"
    finally:
        conn.close()

def waybill_exists(waybill):
    """该运单号是否已被其他物流记录占用。"""
    code = str(waybill or "").strip()
    if not code:
        return False
    ok, _ = ensure_logistics()
    if not ok:
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT waybill FROM {LOGISTICS_TABLE} WHERE waybill=%s", (code,))
            return cur.fetchone() is not None
    finally:
        conn.close()

def create_logistics(order_ids, user_id, user_name, address, delay_second=None, waybill=None):
    """
    商家发货后写入物流信息：
        - 运单号由商家填写（必须传入）
        - 发货时间 = 当前时间
        - 预计送达 = 当前时间 + 默认等待秒数（config.json / set_time_config.py）
    返回 (是否成功, 提示信息, 物流 dict 或 None)
    """
    code = str(waybill or "").strip()
    if not code:
        return False, "缺少快递单号，请由商家填写后再发货", None

    ok, msg = ensure_logistics()
    if not ok:
        return False, msg, None

    delay = load_default_delay() if delay_second is None else int(delay_second)
    now = datetime.now()
    eta = now + timedelta(seconds=delay)
    order_id_text = ",".join(str(x) for x in (order_ids or []))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {LOGISTICS_TABLE} "
                "(waybill, order_ids, user_id, user_name, address, carrier, shipped_at, estimated_arrival) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (code, order_id_text, str(user_id or ""), user_name,
                 address, CARRIER, now, eta),
            )
        conn.commit()
        data = {
            "waybill": code,
            "order_ids": order_ids or [],
            "user_id": str(user_id or ""),
            "user_name": user_name,
            "address": address,
            "carrier": CARRIER,
            "shipped_at": format_datetime(now),
            "estimated_arrival": format_datetime(eta),
            "delay_second": delay,
        }
        return True, f"物流已生成：运单号 {code}，预计送达 {data['estimated_arrival']}", data
    except Exception as e:
        conn.rollback()
        return False, f"物流生成失败：{e}", None
    finally:
        conn.close()

def register_return_logistics(waybill, order_ids, user_id, user_name, address):
    """把平台生成的退货 / 换货运单登记进物流表，使其可按运单号被商家 / 客户查询。

    商家同意退换货时登记：此时用户还没寄出，发货时间留空（物流状态为「待发货」），
    用户点「我已发出」后再由 update_return_logistics_shipped() 补上发货时间。
    关联原订单 ID，商家可凭物权校验查到本店订单的退货物流。
    """
    code = str(waybill or "").strip()
    if not code:
        return False, "缺少退货运单号"
    ok, msg = ensure_logistics()
    if not ok:
        return False, msg

    order_id_text = ",".join(str(x) for x in (order_ids or []))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 退货件尚未寄出：发货 / 预计到仓时间留空，等用户点「我已发出」再补
            cur.execute(
                f"INSERT INTO {LOGISTICS_TABLE} "
                "(waybill, order_ids, user_id, user_name, address, carrier, "
                " shipped_at, estimated_arrival) VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL) "
                "ON DUPLICATE KEY UPDATE order_ids=VALUES(order_ids), user_id=VALUES(user_id), "
                "user_name=VALUES(user_name), address=VALUES(address)",
                (code, order_id_text, str(user_id or ""), user_name, address, CARRIER),
            )
        conn.commit()
        return True, f"退货运单已登记：{code}"
    except Exception as e:
        conn.rollback()
        return False, f"退货运单登记失败：{e}"
    finally:
        conn.close()

def update_return_logistics_shipped(waybill, delay_second=None):
    """用户点「我已发出」后，给退货运单补上发货时间与预计到仓时间。"""
    code = str(waybill or "").strip()
    if not code:
        return False, "缺少退货运单号"
    ok, msg = ensure_logistics()
    if not ok:
        return False, msg

    delay = load_default_delay() if delay_second is None else int(delay_second)
    now = datetime.now()
    eta = now + timedelta(seconds=delay)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {LOGISTICS_TABLE} SET shipped_at=%s, estimated_arrival=%s "
                "WHERE waybill=%s",
                (now, eta, code),
            )
        conn.commit()
        return True, f"退货运单已更新：{code}"
    except Exception as e:
        conn.rollback()
        return False, f"退货运单更新失败：{e}"
    finally:
        conn.close()

def get_logistics(waybill):
    """
    按运单号查询物流，返回 (是否找到, 物流 dict)；
    未找到返回 (False, None)。
    """
    code = str(waybill or "").strip()
    if not code:
        return False, None

    ok, _ = ensure_logistics()
    if not ok:
        return False, None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT waybill, order_ids, user_id, user_name, address, carrier, "
                f"shipped_at, estimated_arrival, delivered_at, create_time "
                f"FROM {LOGISTICS_TABLE} WHERE waybill=%s",
                (code,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return False, None

    shipped_at = row.get("shipped_at")
    eta = row.get("estimated_arrival")
    step, text = compute_status(shipped_at, eta)

    row["status_step"] = step
    row["status_text"] = text
    for key in ("shipped_at", "estimated_arrival", "delivered_at", "create_time"):
        if row.get(key) is not None:
            row[key] = format_datetime(row[key])
    return True, row

def get_logistics_by_user(user_id, limit=20):
    """查询某用户最近的物流记录"""
    ok, _ = ensure_logistics()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT waybill, order_ids, carrier, shipped_at, estimated_arrival, delivered_at "
                f"FROM {LOGISTICS_TABLE} WHERE user_id=%s ORDER BY create_time DESC LIMIT %s",
                (str(user_id), int(limit)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        step, text = compute_status(r.get("shipped_at"), r.get("estimated_arrival"))
        r["status_step"] = step
        r["status_text"] = text
        for key in ("shipped_at", "estimated_arrival", "delivered_at"):
            if r.get(key) is not None:
                r[key] = format_datetime(r[key])
        result.append(r)
    return result
