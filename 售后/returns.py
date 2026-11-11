"""售后模块 —— 退换货流程（退货 / 换货 / 退款）。

流程：
    1. 未发货（订单还没运单 / 物流）：
       - 退款、退货都**无需理由、无需商家同意、不走仲裁**，提交后直接完成；
       - 不允许申请换货（货还没发出，没有换货可言）。
    2. 已发货未签收：可申请退货 / 换货 / 退款（需说明理由），只能「全部退回」。
    3. 已签收：退货 5 分钟内、换货 10 分钟内有效；可申请部分数量退换；退款不限时间窗。
    4. 同一订单允许多次申请，每次都是独立的售后单号
    5. 初始状态 requested（待商家确认），此阶段不生成快递单号
    6. 商家同意退/换货 → 平台生成 7 位 return_waybill，用户凭此单号寄回
    7. 用户填写寄回物流单号并点「我已发出」→ 记录寄出时间，按默认等待时间算预计到仓时间
    8. 当前时间超过预计到仓时间：
       - 退货 → 退换完成（done）
       - 换货 → 商家已签收（received），等商家填写新运单号「重新发货」
    9. 换货：商家签收后填写新的 7 位运单号重新发货 → 换货完成（done），
       新运单登记进物流表，顾客可继续按新单号查物流
    10. 商家拒绝 → 用户可申请平台仲裁（arbitrating）

数据表 cs.return_order：
    id / order_id / user_id / user_name / goodsname
    type              return=退货 / exchange=换货 / refund=退款
    original_waybill 原运单号 / return_waybill 退货(换货)寄回物流单号
    reship_waybill    换货重新发货运单号（商家签收后填写）
    address / reason / carrier / status
    create_time / shipped_at 用户点「我已发出」时间 / estimated_arrival 预计到仓时间
"""
import threading
import uuid
from datetime import datetime, timedelta

from 公共.db_config import get_conn
from 物流.waybill import generate_7digit, load_default_delay
from 物流.repository import (
    create_logistics,
    ensure_logistics,
    register_return_logistics,
    update_return_logistics_shipped,
    waybill_exists,
)
from 物流.status import parse_datetime, format_datetime
from .order import (
    effective_quantity, base_goodsname, order_status, normalize_waybill,
)

RETURN_TABLE = "`return_order`"
CARRIER = "Kaomianbao 速运"

TYPE_TEXTS = {"return": "退货", "exchange": "换货", "refund": "退款"}
VALID_TYPES = tuple(TYPE_TEXTS.keys())

# 退款（商家处理）状态流转：待商家处理 → 退回中 → 派送中 → 商家签收 → 已退款
# 退款被平台仲裁判定支持顾客后，会进入与退货一致的「平台生成单号 → 买家寄回」流程，
# 因此这里也要能显示 approved / shipping 等寄回阶段的状态。
REFUND_STATUS_TEXTS = {
    "requested": "待商家处理",
    "returning": "退回中",
    "delivering": "派送中",
    "received": "商家签收",
    "refunded": "已退款",
    "rejected": "商家已拒绝，可申请平台仲裁",
    "arbitrating": "平台仲裁中",
    "approved": "商家已同意，请凭平台退货单号寄出",
    "shipping": "退货运输中",
    "done": "退款完成",
}
REFUND_FLOW = ["requested", "returning", "delivering", "received", "refunded"]
REFUND_TERMINAL = ("refunded", "rejected")

# 退货 / 换货状态流转：
#   requested 待商家确认 → approved 商家已同意（用户填写退货快递单号并寄出）
#   → shipping 退货运输中 → done 退换完成；任意时刻可 rejected 商家拒绝
#   pending 为历史兼容状态（旧版申请即生成单号），行为与 approved 一致
RETURN_STATUS_TEXTS = {
    "requested": "待商家确认",
    "approved": "商家已同意，请填写寄回物流单号并寄出",
    "pending": "待寄出（请填写快递单号并点击「我已发出」）",
    "shipping": "退货运输中",
    "received": "商家已签收，等待重新发货",
    "done": "退换完成",
    "rejected": "商家已拒绝，可申请平台仲裁",
    "arbitrating": "平台仲裁中",
}
RETURN_WAIT_SHIP = ("approved", "pending")   # 等待用户寄出的状态
# 换货：寄回件已到仓（商家可签收并重新发货）的阶段
RETURN_WAIT_RESHIP = ("shipping", "received")
RETURN_TERMINAL = ("done", "rejected", "arbitrating")
# 进行中的退货 / 换货申请：还没走完，允许用户改主意换类型（换货 ↔ 退货）
RETURN_ACTIVE = ("requested", "pending", "approved", "shipping")

# 时间窗（分钟）：签收后允许退/换货的时间窗口；未签收不受限制
RETURN_WINDOW_MINUTES = 5      # 退货：签收后 5 分钟内
EXCHANGE_WINDOW_MINUTES = 10   # 换货：签收后 10 分钟内

CREATE_RETURN_SQL = """
CREATE TABLE IF NOT EXISTS `return_order` (
    `id`                VARCHAR(32)  NOT NULL                 COMMENT '退换货/退款单号',
    `order_id`          VARCHAR(64)  DEFAULT NULL             COMMENT '原订单ID',
    `user_id`           VARCHAR(64)  DEFAULT NULL             COMMENT '申请用户ID',
    `user_name`         VARCHAR(64)  DEFAULT NULL             COMMENT '申请用户姓名',
    `goodsname`         VARCHAR(255) DEFAULT NULL             COMMENT '商品',
    `owner_id`          VARCHAR(64)  DEFAULT NULL             COMMENT '商品所属商户ID',
    `type`              VARCHAR(16)  DEFAULT 'return'         COMMENT 'return退货/exchange换货/refund退款',
    `quantity`          INT          DEFAULT 1                COMMENT '本次退换数量',
    `original_waybill`  VARCHAR(32)  DEFAULT NULL             COMMENT '原运单号',
    `return_waybill`    VARCHAR(32)  DEFAULT NULL             COMMENT '退货(换货)寄回物流单号',
    `reship_waybill`    VARCHAR(32)  DEFAULT NULL             COMMENT '换货重新发货运单号',
    `address`           VARCHAR(255) DEFAULT NULL             COMMENT '寄回地址',
    `reason`            TEXT                                  COMMENT '原因',
    `carrier`           VARCHAR(64)  DEFAULT 'Kaomianbao 速运' COMMENT '物流公司',
    `status`            VARCHAR(16)  DEFAULT 'pending'        COMMENT 'pending待寄出/shipping运输中/done完成；退款：requested/returning/delivering/received/refunded/rejected',
    `create_time`       DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '申请时间',
    `shipped_at`        DATETIME     DEFAULT NULL             COMMENT '用户点「我已发出」时间',
    `estimated_arrival` DATETIME     DEFAULT NULL             COMMENT '预计到仓时间',
    `update_time`       DATETIME     DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_order` (`order_id`),
    KEY `idx_user` (`user_id`),
    KEY `idx_owner` (`owner_id`),
    KEY `idx_waybill` (`return_waybill`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='退换货/退款表';
"""

# 退款流水：每一笔「真正发生资金变动」的售后（退款完成 / 退货退款完成）记一行，
# 哪怕没有接真实支付网关，也记录金额与去向，便于对账与演示讲解交易闭环。
CREATE_REFUND_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS `refund_ledger` (
    `id`            VARCHAR(32)  NOT NULL                     COMMENT '流水号',
    `return_id`     VARCHAR(32)  DEFAULT NULL                 COMMENT '关联售后单号',
    `order_id`      VARCHAR(64)  DEFAULT NULL                 COMMENT '原订单号',
    `user_id`       VARCHAR(64)  DEFAULT NULL                 COMMENT '顾客ID',
    `user_name`     VARCHAR(64)  DEFAULT NULL                 COMMENT '顾客姓名',
    `owner_id`      VARCHAR(64)  DEFAULT NULL                 COMMENT '商户ID',
    `type`          VARCHAR(16)  DEFAULT 'refund'             COMMENT 'refund退款/return退货退款/exchange换货退款',
    `amount`        DECIMAL(10,2) DEFAULT 0                   COMMENT '退款金额',
    `channel`       VARCHAR(64)  DEFAULT '原路退回(模拟)'       COMMENT '退款去向/渠道',
    `status`        VARCHAR(16)  DEFAULT 'done'               COMMENT 'done已退款',
    `remark`        VARCHAR(255) DEFAULT NULL                 COMMENT '备注',
    `create_time`   DATETIME     DEFAULT CURRENT_TIMESTAMP     COMMENT '记账时间',
    PRIMARY KEY (`id`),
    KEY `idx_return` (`return_id`),
    KEY `idx_order` (`order_id`),
    KEY `idx_user` (`user_id`),
    KEY `idx_owner` (`owner_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='退款流水表';
"""


# ============================ 建表 / 迁移 ============================
def _column_exists(cur, table_name, column_name):
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table_name, column_name),
    )
    return cur.fetchone() is not None


_ensure_done = False
_ensure_lock = threading.Lock()


def ensure_return_table():
    """确保 cs.return_order 存在，并补齐字段。返回 (是否成功, 提示信息)。

    建表 / 补字段属于 DDL，会加元数据锁，因此进程内只执行一次；
    后续调用直接返回，避免每个请求都跑 DDL 造成并发阻塞。
    """
    global _ensure_done
    if _ensure_done:
        return True, "cs.return_order 表已就绪"
    with _ensure_lock:
        if _ensure_done:
            return True, "cs.return_order 表已就绪"
        ok, msg = _ensure_return_table_once()
        if ok:
            _ensure_done = True
        return ok, msg


def _ensure_return_table_once():
    """真正执行建表 / 补字段（仅进程内首次调用时执行）。"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_RETURN_SQL)
            if not _column_exists(cur, "return_order", "type"):
                cur.execute(
                    f"ALTER TABLE {RETURN_TABLE} "
                    "ADD COLUMN `type` VARCHAR(16) DEFAULT 'return' "
                    "COMMENT 'return退货/exchange换货' AFTER `goodsname`"
                )
            if not _column_exists(cur, "return_order", "quantity"):
                cur.execute(
                    f"ALTER TABLE {RETURN_TABLE} "
                    "ADD COLUMN `quantity` INT DEFAULT 1 COMMENT '本次退换数量' AFTER `type`"
                )
            if not _column_exists(cur, "return_order", "owner_id"):
                cur.execute(
                    f"ALTER TABLE {RETURN_TABLE} "
                    "ADD COLUMN `owner_id` VARCHAR(64) DEFAULT NULL COMMENT '商品所属商户ID' AFTER `goodsname`"
                )
                cur.execute(f"ALTER TABLE {RETURN_TABLE} ADD KEY `idx_owner` (`owner_id`)")
            if not _column_exists(cur, "return_order", "reject_reason"):
                cur.execute(
                    f"ALTER TABLE {RETURN_TABLE} "
                    "ADD COLUMN `reject_reason` TEXT COMMENT '商家拒绝理由（说明为什么不通过）' AFTER `reason`"
                )
            if not _column_exists(cur, "return_order", "reship_waybill"):
                cur.execute(
                    f"ALTER TABLE {RETURN_TABLE} "
                    "ADD COLUMN `reship_waybill` VARCHAR(32) DEFAULT NULL "
                    "COMMENT '换货重新发货运单号' AFTER `return_waybill`"
                )
            # 退款流水表（资金变动留痕）
            cur.execute(CREATE_REFUND_LEDGER_SQL)
        conn.commit()
        return True, "cs.return_order / refund_ledger 表已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"退换货表初始化失败：{e}"
    finally:
        conn.close()


# ============================ 工具 ============================
def _gen_return_id():
    # 随机后缀 8 位十六进制：4 位时同一秒内并发提交售后会撞主键（与订单号同理）
    return "RE" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:8].upper()


def _new_return_waybill(cur, user_id, address):
    """生成一个不与现有运单 / 物流冲突的 7 位新物流单号"""
    uid = int(user_id) if str(user_id).strip().isdigit() else 0
    for i in range(20):
        salt = (address or "") + "#" + str(i)
        waybill = f"{generate_7digit(uid, salt):07d}"
        cur.execute(f"SELECT id FROM {RETURN_TABLE} WHERE return_waybill=%s", (waybill,))
        if cur.fetchone():
            continue
        cur.execute("SELECT waybill FROM `logistics` WHERE waybill=%s", (waybill,))
        if cur.fetchone():
            continue
        return waybill
    return f"{generate_7digit(uid, address or ''):07d}"


def format_return_row(row):
    """补上动态状态 / 类型文案，并把时间格式化为 ISO 字符串。"""
    code, text = compute_return_status(row)
    row["status_code"] = code
    row["status_text"] = text
    row["type_text"] = TYPE_TEXTS.get(str(row.get("type") or "return"), "退货")
    row["reject_reason"] = str(row.get("reject_reason") or "")
    try:
        row["quantity"] = int(row.get("quantity") or 1)
    except (TypeError, ValueError):
        row["quantity"] = 1
    for key in ("create_time", "shipped_at", "estimated_arrival", "update_time"):
        if row.get(key) is not None:
            row[key] = format_datetime(row[key])
    return row


def record_refund_ledger(return_id, channel="原路退回(模拟)", remark=None):
    """售后单到达资金终态时记一笔退款流水（幂等：同一售后单只记一次）。

    金额 = 原订单单价 × 本次退换数量（取自 cs.order）。
    触发点：set_after_sale_status 中退款到「refunded/done」、退货/换货到「done」。
    """
    rid = str(return_id or "").strip()
    if not rid:
        return False
    ok, _ = ensure_return_table()
    if not ok:
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {RETURN_TABLE} WHERE id=%s", (rid,))
            row = cur.fetchone()
            if not row:
                return False
            # 幂等：已记过则跳过
            cur.execute("SELECT id FROM `refund_ledger` WHERE return_id=%s", (rid,))
            if cur.fetchone():
                return True
            rtype = str(row.get("type") or "return")
            order_id = str(row.get("order_id") or "")
            # 取原订单金额与数量
            amount = 0.0
            restock_qty = 0
            restock_sku = ""
            if order_id:
                cur.execute(
                    "SELECT price, quantity, sku_id, pay_status FROM `order` WHERE id=%s",
                    (order_id,),
                )
                o = cur.fetchone()
                if o:
                    try:
                        price = float(o.get("price") or 0)
                    except (TypeError, ValueError):
                        price = 0.0
                    try:
                        qty = int(o.get("quantity") or row.get("quantity") or 1)
                    except (TypeError, ValueError):
                        qty = 1
                    amount = round(price * qty, 2)
                    # 只有「已支付」的订单才真正扣减过实物库存，退款时才有货可回补；
                    # 未支付订单的预占在下单取消时已释放，不能再加一次，否则库存虚增。
                    if str(o.get("pay_status") or "") == "paid":
                        restock_sku = str(o.get("sku_id") or "")
                        try:
                            restock_qty = int(row.get("quantity") or 0) or qty
                        except (TypeError, ValueError):
                            restock_qty = qty
            ledger_id = "RF" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:8].upper()
            cur.execute(
                "INSERT INTO `refund_ledger` "
                "(id, return_id, order_id, user_id, user_name, owner_id, type, amount, channel, status, remark) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'done', %s)",
                (ledger_id, rid, order_id or None,
                 str(row.get("user_id") or ""), str(row.get("user_name") or ""),
                 str(row.get("owner_id") or ""), rtype, amount, channel,
                 remark or f"{TYPE_TEXTS.get(rtype, '售后')}完成退款"),
            )
        conn.commit()
        # 资金已退且商品真的扣过库存 -> 回补可售库存（放在事务外，避免跨连接嵌套事务）
        if restock_sku and restock_qty > 0:
            try:
                from 公共 import inventory
                inventory.restock(restock_sku, restock_qty, reason=f"售后退款回补({rid})")
            except Exception:
                pass
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def get_refund_ledger(owner_id=None, user_id=None, limit=200):
    """查询退款流水（平台 / 商户 / 顾客视角）。"""
    ok, _ = ensure_return_table()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            where, params = [], []
            if owner_id not in (None, ""):
                where.append("owner_id=%s"); params.append(str(owner_id))
            if user_id not in (None, ""):
                where.append("user_id=%s"); params.append(str(user_id))
            sql = "SELECT * FROM `refund_ledger`"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY create_time DESC LIMIT %s"
            params.append(int(limit))
            cur.execute(sql, params)
            rows = cur.fetchall()
        for r in rows:
            if r.get("create_time") is not None:
                r["create_time"] = format_datetime(r["create_time"])
        return rows
    finally:
        conn.close()


def compute_return_status(row, now=None):
    """状态文案。

    文案唯一来源是 RETURN_STATUS_TEXTS / REFUND_STATUS_TEXTS，避免同一状态两套说法
    （例如 approved 只能有一种写法：「商家已同意，请凭平台退货单号寄出」）。

    退货/换货：shipping 会按「预计到仓时间」动态判定 ——
        退货到仓即 done；换货到仓先变 received（商家已签收，等重新发货），
        商家填了新运单号后才 done。
    退款：由商家处理驱动（returning/delivering/received/refunded）。
    """
    status = str(row.get("status") or "requested")
    rtype = str(row.get("type") or "return")
    if rtype == "refund":
        return status, REFUND_STATUS_TEXTS.get(status, "待商家处理")

    # shipping 要按「预计到仓时间」动态判定是否已完成，不能直接查表返回
    if status != "shipping" and status in RETURN_STATUS_TEXTS:
        return status, RETURN_STATUS_TEXTS[status]

    now = now or datetime.now()
    eta = parse_datetime(row.get("estimated_arrival"))
    if eta is not None and now >= eta:
        # 换货：寄回件到仓后要等商家重新发货，不能直接算完成
        if rtype == "exchange" and not str(row.get("reship_waybill") or "").strip():
            return "received", RETURN_STATUS_TEXTS["received"]
        return "done", RETURN_STATUS_TEXTS["done"]
    return "shipping", RETURN_STATUS_TEXTS["shipping"]


# ============================ 原订单查询 / 校验 ============================
def get_order_with_logistics(order_id):
    """查询订单及其物流信息；订单不存在返回 None。"""
    ensure_logistics()
    oid = str(order_id or "").strip()
    if not oid:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.goodsname, o.product_id, o.quantity, o.price, o.name, o.user_id, o.address, o.waybill, "
                "o.group_id, o.merchant_id, o.merchant_name, o.create_time, "
                "l.shipped_at, l.estimated_arrival, l.carrier "
                "FROM `order` o LEFT JOIN `logistics` l ON o.waybill = l.waybill "
                "WHERE o.id=%s",
                (oid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def check_return_eligibility(order, return_type="return"):
    """
    校验原订单物流状态与时间窗：
        - 未发货（step=0）：退款 / 退货直接受理（无需理由、无需商家同意）；不允许换货
        - 已发货未签收：可退/换/退款，不受时间窗限制（未签收只能全部退回）
        - 已签收：退货 5 分钟内、换货 10 分钟内有效；退款不限时间窗
        - 无物流记录的历史订单默认视为已签收（老数据兼容）
    返回 (是否可申请, step, text, 提示, received, received_at)
        received: bool 是否已签收
        received_at: datetime 签收时间（未签收为 None）
    """
    if order is None:
        return False, 0, "不存在", "订单不存在", False, None

    step, text = order_status(order)
    received = (step >= 4)
    received_at = None
    if received:
        # 签收时间 = 物流预计送达时间（到达即视为签收）
        received_at = parse_datetime(order.get("estimated_arrival"))

    # 未发货：不能申请换货（货还没发出）；退款 / 退货一律直接受理
    if step == 0 and return_type == "exchange":
        return (False, step, text,
                "商品尚未发货，不能申请换货；未发货可直接申请退款或退货",
                received, received_at)

    # 退款不限制物流状态与时间窗
    if return_type == "refund":
        if step == 0:
            return True, step, text, "商品尚未发货，可直接退款（无需理由）", received, received_at
        return True, step, text, "可以申请退款", received, received_at

    if not received:
        if step == 0:
            return True, step, text, "商品尚未发货，可直接退货（无需理由）", received, received_at
        # 已发货未签收：可退可换，不受时间窗限制，但只能全部退回
        return True, step, text, "未签收，可以申请退换货（需全部退回）", received, received_at

    # 已签收：检查时间窗
    now = datetime.now()
    if received_at is None:
        # 老订单无签收时间数据，默认视为刚签收，允许申请
        return True, step, text, "可以申请", received, received_at

    elapsed = (now - received_at).total_seconds() / 60.0  # 已签收经过的分钟数
    if return_type == "exchange" and elapsed > EXCHANGE_WINDOW_MINUTES:
        return (False, step, text,
                f"换货需在签收后 {EXCHANGE_WINDOW_MINUTES} 分钟内申请，当前已超过",
                received, received_at)
    if return_type == "return" and elapsed > RETURN_WINDOW_MINUTES:
        return (False, step, text,
                f"退货需在签收后 {RETURN_WINDOW_MINUTES} 分钟内申请，当前已超过",
                received, received_at)
    return True, step, text, "可以申请", received, received_at


# ============================ 申请退货 / 换货 ============================
def _find_owner_id(order, product_name):
    """找到商品所属商户 ID：优先用下单时的商家快照，再按商品当前归属兜底。"""
    # 下单时已固化商家归属，即使商品后来被删除/转交也不影响售后路由
    mid = str(order.get("merchant_id") or "")
    if mid:
        return mid
    from 公共 import store
    pid = str(order.get("product_id") or "")
    if pid:
        product = store.get_product(pid)
        if product:
            return str(product.get("owner_id") or "")
    name = base_goodsname(product_name)
    for product in store.list_products():
        if base_goodsname(product.get("name")) == name:
            return str(product.get("owner_id") or "")
    return ""


# ============================ 申请退货 / 换货 / 退款 ============================
def create_return(order_id, user, reason="", return_type="return", quantity=None):
    """
    申请退货 / 换货 / 退款：
        - 未发货：退款 / 退货无需理由、无需商家同意、不走仲裁，提交即完成；不允许换货；
          支持按件退（一件一件退，退了就减少可退数量）
        - 已发货未签收：理由必填，可退/换/退款，只能「全部退回」
        - 已签收：理由必填；退货 5 分钟内、换货 10 分钟内有效；可申请部分数量退
        - 同一订单允许多次申请，每次都是独立的售后单号；商家同意退/换货时
          由平台生成新的 7 位 return_waybill，用户凭此单号寄回
    return_type: "return"（退货） / "exchange"（换货） / "refund"（退款）
    quantity: 本次退/换/退数量；不传则默认剩余全部
    返回 (是否成功, 提示信息, 退换货/退款 dict 或 None)
    """
    if return_type not in VALID_TYPES:
        return_type = "return"
    is_refund = (return_type == "refund")
    type_text = TYPE_TEXTS[return_type]

    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    order = get_order_with_logistics(order_id)
    if order is None:
        return False, "订单不存在", None
    if str(order.get("user_id")) != str(user.get("id")) and not user.get("is_admin"):
        return False, "无权操作该订单", None

    # 校验物流状态与时间窗（退款不限物流状态；退货/换货受时间窗约束）
    eligible, step, _text, tip, received, _received_at = \
        check_return_eligibility(order, return_type)
    reason = str(reason or "").strip()
    if not eligible:
        # 用户改主意时（如已申请换货、现在要退货）：直接把进行中的那笔申请改成新类型，
        # 不重新校验时间窗
        changed, changed_msg, changed_ret = change_active_return_type(
            order_id, return_type, user_id=user.get("id"), reason=reason
        )
        if changed:
            return True, changed_msg, changed_ret
        return False, tip, None

    # 未发货：退款 / 退货都无需理由、无需商家同意、不走仲裁，提交即完成
    unshipped = (step == 0)
    if not unshipped and not reason:
        return False, f"请填写{type_text}理由（无论是否已收货都需要说明原因）", None
    if unshipped and not reason:
        reason = f"商品尚未发货，{type_text}无需理由"

    order_qty = effective_quantity(order)
    product_name = base_goodsname(order.get("goodsname")) or (order.get("goodsname") or "")
    address = order.get("address") or ""
    owner_id = _find_owner_id(order, product_name)
    now = datetime.now()
    if unshipped:
        # 货还没发出：直接置为终态（退款=已退款 / 退货=退换完成），不进商家待办、不走仲裁
        init_status = "refunded" if is_refund else "done"
    else:
        # 退货 / 换货 / 退款一律先进入「待商家确认」，商家同意后才进入后续环节
        init_status = "requested"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 已申请的退换/退款数量（商家已拒绝的不占额度）
            cur.execute(
                f"SELECT COALESCE(SUM(quantity), 0) AS q FROM {RETURN_TABLE} "
                "WHERE order_id=%s AND status <> 'rejected'",
                (str(order_id),),
            )
            used = int((cur.fetchone() or {}).get("q") or 0)
            remaining = max(0, order_qty - used)
            if remaining <= 0:
                # 数量已被另一笔申请占满（如整单已申请换货）：若用户现在要的是另一种类型，
                # 直接把那笔申请改成新类型，而不是报「已全部申请退换」
                changed, changed_msg, changed_ret = change_active_return_type(
                    str(order_id), return_type, user_id=user.get("id"), reason=reason
                )
                if changed:
                    return True, changed_msg, changed_ret
                return False, f"该订单（{product_name} ×{order_qty}）已全部申请退换", None

            try:
                req = int(quantity) if quantity not in (None, "", 0) else remaining
            except (TypeError, ValueError):
                req = remaining
            if req < 1:
                req = 1

            # 已发货未签收：只能全部退回；未发货（step=0）允许按件退，方便一件一件退
            if not received and not unshipped and req != order_qty:
                return False, (
                    f"商品已发出但尚未签收，只能申请全部退回（共 {order_qty} 件）；"
                    f"签收后才可以申请部分数量退换"
                ), None

            if req > remaining:
                return False, (
                    f"该订单共 {order_qty} 件，已申请 {used} 件，最多还能申请 {remaining} 件"
                ), None

            # 允许同订单多次申请：不再阻止已有进行中的售后单，只要还有可退数量即可
            return_id = _gen_return_id()
            # 申请阶段一律不生成快递单号：商家同意后由平台生成 7 位 return_waybill
            cur.execute(
                f"INSERT INTO {RETURN_TABLE} "
                "(id, order_id, user_id, user_name, goodsname, owner_id, type, quantity, original_waybill, "
                " return_waybill, address, reason, carrier, status, create_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (return_id, str(order_id), str(user.get("id")), user.get("name"),
                 product_name, owner_id, return_type, req, order.get("waybill"), None,
                 address, reason, CARRIER, init_status, now),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"{type_text}申请失败：{e}", None
    finally:
        conn.close()

    found, data = get_return(return_id)
    if unshipped:
        # 未发货：无需理由、无需商家同意、不走仲裁，提交即完成
        message = (f"商品尚未发货，已直接为你完成{type_text}"
                   f"（订单 {order_id}，{product_name} ×{req}，售后单号 {return_id}）。"
                   f"无需理由、无需商家同意、不走仲裁。")
    elif is_refund:
        message = (f"已为订单 {order_id}（{product_name} ×{req}）提交退款申请，"
                   f"等待商家处理（售后单号 {return_id}）")
    elif not received:
        # 未签收：整单退回，原运单号不变，只更新售后状态
        message = (f"已为订单 {order_id}（{product_name} ×{req}）提交{type_text}申请，"
                   f"商品尚未签收，将整单退回（原运单号 {order.get('waybill') or '—'} 不变，"
                   f"只更新售后状态），等待商家确认（售后单号 {return_id}）")
    else:
        message = (f"已为订单 {order_id}（{product_name} ×{req}）提交{type_text}申请，"
                   f"等待商家确认（售后单号 {return_id}）。商家同意后，平台会生成退货快递单号，"
                   f"请在「我的订单-退换货记录」里填写该单号并点击「我已发出」。")
    return True, message, data


# ============================ 填写快递单号 ============================
def update_return_waybill(return_id, waybill, user_id=None):
    """把客服给出的快递单号填入订单信息。返回 (是否成功, 提示, dict)。"""
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "退换货单不存在", None
    if user_id and str(row.get("user_id")) != str(user_id):
        return False, "无权操作该退换货单", None
    if str(row.get("type") or "") != "refund" and \
            str(row.get("status") or "") not in RETURN_WAIT_SHIP:
        return False, f"当前状态为「{row.get('status_text')}」，暂不能填写快递单号", row

    code = str(waybill or "").strip()
    if not code:
        return False, "请填写快递单号", row
    if len(code) > 32:
        return False, "快递单号过长（最多 32 位）", row

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {RETURN_TABLE} SET return_waybill=%s WHERE id=%s",
                (code, return_id),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"快递单号保存失败：{e}", None
    finally:
        conn.close()

    found, data = get_return(return_id)
    return True, f"快递单号已保存：{code}", data


# ============================ 用户点击「我已发出」 ============================
def mark_return_shipped(return_id, user_id=None, waybill=None):
    """
    用户点击「我已发出」：记录寄出时间，并按默认等待时间计算预计到仓时间。
    可同时把客服给出的快递单号填入。返回 (是否成功, 提示信息, dict 或 None)
    """
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "退换货单不存在", None
    if user_id and str(row.get("user_id")) != str(user_id):
        return False, "无权操作该退换货单", None

    if str(row.get("status") or "") not in RETURN_WAIT_SHIP:
        return False, f"当前状态为「{row.get('status_text')}」，无需重复寄出", row

    code = str(waybill or "").strip() or str(row.get("return_waybill") or "").strip()
    if not code:
        return False, "请先填写你寄回的快递单号，再点击「我已发出」", row
    now = datetime.now()
    eta = now + timedelta(seconds=load_default_delay())

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {RETURN_TABLE} SET status='shipping', shipped_at=%s, "
                "estimated_arrival=%s, return_waybill=%s WHERE id=%s",
                (now, eta, code, return_id),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"确认寄出失败：{e}", None
    finally:
        conn.close()

    # 同步退货物流：补上发货 / 预计到仓时间，客户与商家都能按单号查到运输状态
    update_return_logistics_shipped(code)

    found, data = get_return(return_id)
    return True, (
        f"已确认寄出（快递单号 {code}），预计 {format_datetime(eta)} 完成退换"
    ), data


# ============================ 换货：商家签收后重新发货 ============================
def reship_exchange(return_id, waybill, owner_id=None, product_names=None):
    """换货：商家收到顾客寄回的商品后，填写新的 7 位运单号重新发货。

    只有换货单、且寄回件已到仓（shipping 运输中 / received 已签收）时才能操作；
    新运单号会登记进物流表，顾客可继续按新单号查询这次换货的物流。
    返回 (是否成功, 提示信息, dict 或 None)。
    """
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "售后单不存在", None
    if owner_id is not None and not _owns_return(row, owner_id, product_names):
        return False, "无权处理该售后单", row
    if str(row.get("type") or "") != "exchange":
        return False, "只有换货申请才需要重新发货", row
    if str(row.get("status_code") or "") not in RETURN_WAIT_RESHIP:
        return False, f"该换货单当前为「{row.get('status_text')}」，暂不能重新发货", row

    code = normalize_waybill(waybill)
    if not code:
        return False, "重新发货的快递单号必须是 7 位数字", row
    if code == str(row.get("return_waybill") or "").strip():
        return False, "重新发货请使用新的快递单号（不能与顾客寄回的单号相同）", row
    if waybill_exists(code):
        return False, f"快递单号 {code} 已被使用，请更换", row

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {RETURN_TABLE} SET status='done', reship_waybill=%s WHERE id=%s",
                (code, return_id),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"重新发货失败：{e}", None
    finally:
        conn.close()

    # 换货完成（重新发货）：记录退款/换货流水（货款退回 + 新货发出）
    try:
        record_refund_ledger(return_id, remark="换货完成（重发）")
    except Exception:
        pass

    # 新运单登记进物流表：顾客 / 商家都能按新单号查到这次换货的物流
    create_logistics(
        [row.get("order_id")], row.get("user_id"), row.get("user_name"),
        row.get("address"), waybill=code,
    )

    found, data = get_return(return_id)
    return True, f"换货已重新发货（新运单号 {code}），售后单 {return_id} 已完成", data


# ============================ 查询 ============================
def get_return(return_id):
    """按退换货单号 / 物流单号查询，返回 (是否找到, dict)。"""
    ok, _ = ensure_return_table()
    if not ok:
        return False, None
    rid = str(return_id or "").strip()
    if not rid:
        return False, None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {RETURN_TABLE} WHERE id=%s OR return_waybill=%s",
                (rid, rid),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return False, None
    return True, format_return_row(row)


def get_returns_by_user(user_id):
    """查询某用户的全部退换货记录（退货 + 换货）。"""
    ok, _ = ensure_return_table()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {RETURN_TABLE} WHERE user_id=%s ORDER BY create_time DESC",
                (str(user_id),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [format_return_row(r) for r in rows]


# 这些售后单会阻止商家发货：
#   requested / pending / approved / shipping / arbitrating —— 还没了结，
#     未签收只能整单退回、原运单号不变，先发货会把售后流程搞乱；
#   done / refunded —— 未发货订单的退款 / 退货提交即完成，货已经不用再发了。
BLOCK_SHIP_STATUSES = (
    "requested", "pending", "approved", "shipping", "arbitrating", "done", "refunded",
)


def open_returns_by_order(order_ids):
    """批量查这些订单里「未了结」的售后单，返回 {订单ID: 售后行}（每单取最新一条）。

    供商家订单列表标记「不可发货」、以及发货接口拦截使用，避免逐单查库。
    """
    ok, _ = ensure_return_table()
    if not ok:
        return {}
    ids = [str(x) for x in (order_ids or []) if str(x or "").strip()]
    if not ids:
        return {}

    ph = ",".join(["%s"] * len(ids))
    sph = ",".join(["%s"] * len(BLOCK_SHIP_STATUSES))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {RETURN_TABLE} WHERE order_id IN ({ph}) "
                f"AND status IN ({sph}) ORDER BY create_time DESC",
                ids + list(BLOCK_SHIP_STATUSES),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result = {}
    for row in rows:
        oid = str(row.get("order_id") or "")
        if oid and oid not in result:      # 已按时间倒序，第一条即最新
            result[oid] = format_return_row(row)
    return result


def find_blocking_return(order_id):
    """该订单是否有「未了结」的售后单会阻止发货；有则返回最新一条，否则 None。"""
    return open_returns_by_order([order_id]).get(str(order_id or "").strip())


def ship_block_message(row):
    """阻止发货时给商家的提示。"""
    if not row:
        return ""
    if str(row.get("status_code") or "") in ("done", "refunded"):
        # 未发货订单的退款 / 退货提交即完成，货不用再发了
        return (f"该订单的{row.get('type_text') or '售后'}（{row.get('id')}）已完成，"
                f"无需再发货")
    return (f"该订单已有进行中的{row.get('type_text') or '售后'}申请"
            f"（{row.get('id')}，{row.get('status_text')}），请先处理售后后再发货")


# ============================ 换货 ↔ 退货 互改 ============================
def get_active_return(order_id, user_id=None, types=None):
    """该订单下仍在进行中（未完成 / 未拒绝）的售后单，最新的一条；没有返回 None。

    types 可限定类型，如 ("exchange",) 只看换货申请。
    """
    ok, _ = ensure_return_table()
    if not ok:
        return None
    oid = str(order_id or "").strip()
    if not oid:
        return None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {RETURN_TABLE} WHERE order_id=%s ORDER BY create_time DESC",
                (oid,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    want = set(types) if types else None
    for row in rows:
        rtype = str(row.get("type") or "return")
        if want is not None and rtype not in want:
            continue
        if str(row.get("status") or "") not in RETURN_ACTIVE:
            continue
        if user_id is not None and str(row.get("user_id")) != str(user_id):
            continue
        return format_return_row(row)
    return None


def change_return_type(return_id, new_type, user_id=None, reason=None):
    """把一笔进行中的换货 / 退货申请改成另一种（如：换货 → 退货）。

    只改类型（顺便刷新申请理由），数量、快递单号、进度都不动，也不重新校验
    时间窗与可退数量 —— 用户只是把已经提交的申请换个类型，不需要重新满足申请条件。
    返回 (是否成功, 提示信息, dict 或 None)。
    """
    if new_type not in ("return", "exchange"):
        return False, "只支持在退货 / 换货之间切换类型", None

    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "退换货单不存在", None
    if user_id is not None and str(row.get("user_id")) != str(user_id):
        return False, "无权操作该退换货单", None

    old_type = str(row.get("type") or "return")
    if old_type == "refund":
        return False, "退款申请不能改成退货 / 换货", row
    if old_type == new_type:
        return False, f"该申请已经是{TYPE_TEXTS[new_type]}", row
    if str(row.get("status") or "") not in RETURN_ACTIVE:
        return False, f"该申请当前为「{row.get('status_text')}」，无法再改类型", row

    reason = str(reason or "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if reason:
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET type=%s, reason=%s WHERE id=%s",
                    (new_type, reason, return_id),
                )
            else:
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET type=%s WHERE id=%s",
                    (new_type, return_id),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"修改售后类型失败：{e}", None
    finally:
        conn.close()

    found, data = get_return(return_id)
    wb = str(data.get("return_waybill") or "").strip()
    if wb and wb != str(data.get("original_waybill") or "").strip():
        tail = f"凭平台退货单号 {wb} 寄回即可。"
    elif wb:
        tail = f"原运单号 {wb} 不变，退回流程继续生效。"
    else:
        tail = "等待商家确认；商家同意后平台会生成退货快递单号。"
    msg = (
        f"已把订单 {data.get('order_id')}（{data.get('goodsname')} ×{data.get('quantity')}）的"
        f"{TYPE_TEXTS.get(old_type, '售后')}申请改为{TYPE_TEXTS[new_type]}申请"
        f"（售后单号 {return_id}）。{tail}"
    )
    return True, msg, data


def change_active_return_type(order_id, new_type, user_id=None, reason=None):
    """把该订单下进行中的「另一种」售后申请改成 new_type（换货 → 退货 等）。

    没有可改的申请时返回 (False, 提示, None)。
    """
    if new_type not in ("return", "exchange"):
        return False, "只支持在退货 / 换货之间切换类型", None
    other = "exchange" if new_type == "return" else "return"
    active = get_active_return(order_id, user_id=user_id, types=(other,))
    if not active:
        return False, "该订单没有可改类型的进行中申请", None
    return change_return_type(active["id"], new_type, user_id=user_id, reason=reason)


def delete_return(return_id):
    """删除退换货记录（管理员 / 测试用）。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {RETURN_TABLE} WHERE id=%s", (return_id,))
        conn.commit()
        return True, "已删除"
    except Exception as e:
        conn.rollback()
        return False, f"删除失败：{e}"
    finally:
        conn.close()


# ============================ 用户申请平台仲裁 ============================
def request_arbitration(return_id, user_id=None):
    """用户对被商家拒绝的退货/换货申请平台介入仲裁。

    仅当状态为 rejected 时可申请；申请后状态变为 arbitrating（平台仲裁中）。
    返回 (是否成功, 提示, dict)。
    """
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "退换货单不存在", None
    if user_id and str(row.get("user_id")) != str(user_id):
        return False, "无权操作该退换货单", None
    if str(row.get("status") or "") != "rejected":
        return False, f"当前状态为「{row.get('status_text')}」，无需申请仲裁", row

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {RETURN_TABLE} SET status='arbitrating' WHERE id=%s",
                (return_id,),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"申请仲裁失败：{e}", None
    finally:
        conn.close()

    found, data = get_return(return_id)
    return True, f"已提交平台仲裁（售后单号 {return_id}），平台会尽快介入处理", data


# ============================ 商家侧：退款处理 ============================
def _owns_return(row, owner_id, product_names=None):
    """判断一条售后单是否属于该商户（优先 owner_id，老数据按商品名兜底）。"""
    if owner_id is not None and str(row.get("owner_id") or "") == str(owner_id):
        return True
    if product_names:
        names = {base_goodsname(n) for n in product_names}
        return base_goodsname(row.get("goodsname")) in names
    return False


def list_returns_for_owner(owner_id, product_names=None, types=None):
    """某个商户的售后单（退货/换货/退款）。types 可传 ("refund",) 只看退款。"""
    ok, _ = ensure_return_table()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {RETURN_TABLE}")
            rows = cur.fetchall()
    finally:
        conn.close()

    want = set(types) if types else None
    result = []
    for row in rows:
        if want is not None and str(row.get("type") or "return") not in want:
            continue
        if _owns_return(row, owner_id, product_names):
            result.append(format_return_row(row))
    result.sort(key=lambda r: str(r.get("create_time") or ""), reverse=True)
    return result


def next_refund_status(current):
    """退款状态机：requested → returning → delivering → received → refunded。"""
    current = str(current or "requested")
    if current in REFUND_FLOW:
        idx = REFUND_FLOW.index(current)
        return REFUND_FLOW[min(idx + 1, len(REFUND_FLOW) - 1)]
    return "returning"


def set_after_sale_status(return_id, status, owner_id=None, product_names=None,
                          reject_reason=""):
    """商家处理售后单（退货 / 换货 / 退款）。

    退款：requested → returning → delivering → received → refunded（可 rejected），
          status 传 "next" 自动推进；
    退货/换货：requested（待商家确认）→ approved（同意，用户自行寄回）/ rejected（拒绝）。

    拒绝时**必须**填写拒绝理由（reject_reason），说明为什么不通过 —— 该理由会展示给
    顾客，也是顾客申请平台仲裁时平台判定的依据。
    返回 (是否成功, 提示, dict)。
    """
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "售后单不存在", None
    if owner_id is not None and not _owns_return(row, owner_id, product_names):
        return False, "无权处理该售后单", row

    rtype = str(row.get("type") or "return")
    current = str(row.get("status") or "requested")
    status = str(status or "").strip()

    if rtype == "refund":
        if status == "next":
            status = next_refund_status(current)
        if status not in REFUND_STATUS_TEXTS:
            return False, f"不支持的退款状态：{status}", row
    else:
        # 退货 / 换货：只能在「待商家确认」阶段同意或拒绝
        if current != "requested":
            return False, f"该{TYPE_TEXTS.get(rtype, '售后')}单当前为「{row.get('status_text')}」，无需再次确认", row
        if status == "next":
            status = "approved"
        if status not in ("approved", "rejected"):
            return False, f"不支持的{TYPE_TEXTS.get(rtype, '售后')}处理动作：{status}", row

    # 拒绝必须说明理由：该理由要展示给顾客，并作为平台仲裁的判定依据
    reject_reason = str(reject_reason or "").strip()
    if status == "rejected" and not reject_reason:
        return False, f"请填写拒绝理由（说明为什么不通过这次{TYPE_TEXTS.get(rtype, '售后')}申请）", row

    # 商家同意退换货时：已签收 → 平台生成新的退货快递单号（用户凭单号寄回）；
    # 未签收 → 整单退回，原运单号不变，只更新售后状态（用户无需寄回）
    order_received = False
    if status == "approved" and rtype in ("return", "exchange"):
        _order = get_order_with_logistics(row.get("order_id"))
        _step, _ = order_status(_order if isinstance(_order, dict) else {})
        order_received = (_step >= 4)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if status == "approved" and rtype in ("return", "exchange"):
                if order_received:
                    # 已签收：平台生成退货快递单号
                    waybill = _new_return_waybill(cur, row.get("user_id"), row.get("address"))
                    cur.execute(
                        f"UPDATE {RETURN_TABLE} SET status=%s, return_waybill=%s WHERE id=%s",
                        (status, waybill, return_id),
                    )
                else:
                    # 未签收：原运单号不变，直接进入退回运输并按时长自动完成
                    now = datetime.now()
                    eta = now + timedelta(seconds=load_default_delay())
                    cur.execute(
                        f"UPDATE {RETURN_TABLE} SET status='shipping', return_waybill=%s, "
                        "shipped_at=%s, estimated_arrival=%s WHERE id=%s",
                        (str(row.get("original_waybill") or "").strip() or None,
                         now, eta, return_id),
                    )
                    # 未签收即时退货：货款直接退回，记录退款流水
                    try:
                        record_refund_ledger(return_id, remark="未签收即时退货退款")
                    except Exception:
                        pass
            else:
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET status=%s, reject_reason=%s WHERE id=%s",
                    (status, reject_reason or str(row.get("reject_reason") or ""), return_id),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"售后状态更新失败：{e}", None
    finally:
        conn.close()

    # 资金终态：记退款流水（幂等）。退款到 refunded/done，退货/换货到 done 视为退款完成。
    if (rtype == "refund" and status in ("refunded", "done")) or \
       (rtype in ("return", "exchange") and status == "done"):
        try:
            record_refund_ledger(return_id)
        except Exception:
            pass

    # 平台新生成的退货单号登记进物流表，使商家 / 客户都能按运单号查询这笔退货物流
    if status == "approved" and rtype in ("return", "exchange") and order_received:
        register_return_logistics(
            waybill, [row.get("order_id")], row.get("user_id"),
            row.get("user_name"), row.get("address"),
        )

    found, data = get_return(return_id)
    return True, f"售后单 {return_id} 已更新为「{data.get('status_text')}」", data


# 兼容旧名称
def set_refund_status(return_id, status, owner_id=None, product_names=None):
    return set_after_sale_status(
        return_id, status, owner_id=owner_id, product_names=product_names
    )


# ============================ 平台仲裁 ============================
def list_arbitrations():
    """全部「平台仲裁中」的售后单（最新在前）。"""
    ok, _ = ensure_return_table()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {RETURN_TABLE} WHERE status='arbitrating' "
                "ORDER BY update_time DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [format_return_row(r) for r in rows]


def search_returns(keyword="", status=None, limit=200):
    """平台侧按关键词查售后单（含「平台仲裁中」），便于仲裁期间核查进度。

    keyword 模糊匹配 售后单号 / 原订单号 / 顾客姓名 / 商品名；
    status 可指定状态（如 "arbitrating"）；都为空时返回最近的全部记录。
    """
    ok, _ = ensure_return_table()
    if not ok:
        return []
    kw = str(keyword or "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = f"SELECT * FROM {RETURN_TABLE}"
            params = []
            if kw:
                sql += (" WHERE id LIKE %s OR order_id LIKE %s OR user_name LIKE %s "
                        "OR goodsname LIKE %s OR return_waybill LIKE %s")
                params = [f"%{kw}%"] * 5
            sql += " ORDER BY update_time DESC LIMIT %s"
            params.append(int(limit))
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [format_return_row(r) for r in rows]
    st = str(status or "").strip()
    if st:
        items = [r for r in items if str(r.get("status_code") or "") == st]
    return items


def resolve_arbitration(return_id, favor_user=True, note=""):
    """平台处理一笔「仲裁中」的售后单。

    favor_user=True  支持顾客：等价于商家同意，退货 / 换货 / 退款口径一致 ——
                     已签收 → 平台生成 7 位退货单号，状态置为「商家已同意，
                     请凭平台退货单号寄出」，等买家填单号寄出（不显示「退回中」）；
                     未签收 → 原运单号不变、整单退回；
    favor_user=False 支持商家：维持商家拒绝，必须填写 note（拒绝理由，展示给顾客）。

    note 会追加到 reason 的「【平台仲裁】…」里。
    返回 (是否成功, 提示信息, dict 或 None)。
    """
    ok, msg = ensure_return_table()
    if not ok:
        return False, msg, None

    found, row = get_return(return_id)
    if not found:
        return False, "售后单不存在", None
    if str(row.get("status") or "") != "arbitrating":
        return False, f"该售后单当前为「{row.get('status_text')}」，不在仲裁中", row

    note = str(note or "").strip()
    reason = str(row.get("reason") or "")
    if note:
        reason = (reason + "\n" if reason else "") + f"【平台仲裁】{note}"

    if not favor_user:
        # 判定支持商家：维持拒绝，并把平台给出的理由记为拒绝理由展示给顾客
        if not note:
            return False, "判定支持商家必须填写理由（说明为什么不通过）", row
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET status='rejected', reason=%s, "
                    "reject_reason=%s WHERE id=%s",
                    (reason, note or str(row.get("reject_reason") or ""), return_id),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, f"仲裁处理失败：{e}", None
        finally:
            conn.close()
        found, data = get_return(return_id)
        return True, f"已判定支持商家（售后单 {return_id} 维持拒绝），顾客可继续申诉", data

    # 支持顾客：三种类型（退货 / 换货 / 退款）统一口径，与「商家同意」完全一致：
    #   已签收 → 平台生成退货单号，状态置为「商家已同意，请凭平台退货单号寄出」，等买家填单号寄出；
    #   未签收 → 原运单号不变，整单退回（无需买家寄回）。
    order = get_order_with_logistics(row.get("order_id"))
    step, _ = order_status(order if isinstance(order, dict) else {})
    order_received = (step >= 4)
    waybill = ""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if order_received:
                waybill = _new_return_waybill(cur, row.get("user_id"), row.get("address"))
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET status='approved', return_waybill=%s, "
                    "reject_reason='', reason=%s WHERE id=%s",
                    (waybill, reason, return_id),
                )
            else:
                now = datetime.now()
                eta = now + timedelta(seconds=load_default_delay())
                waybill = str(row.get("original_waybill") or "").strip()
                cur.execute(
                    f"UPDATE {RETURN_TABLE} SET status='shipping', return_waybill=%s, "
                    "shipped_at=%s, estimated_arrival=%s, reject_reason='', reason=%s "
                    "WHERE id=%s",
                    (waybill or None, now, eta, reason, return_id),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"仲裁处理失败：{e}", None
    finally:
        conn.close()

    if order_received and waybill:
        register_return_logistics(
            waybill, [row.get("order_id")], row.get("user_id"),
            row.get("user_name"), row.get("address"),
        )

    found, data = get_return(return_id)
    if order_received:
        tip = (f"已判定支持顾客（售后单 {return_id}），平台已生成退货单号 {waybill}，"
               f"请在「我的订单 - 退换货记录」中凭该单号点「我已发出」寄回")
    else:
        tip = (f"已判定支持顾客（售后单 {return_id}），商品未签收，"
               f"原运单号 {waybill or '—'} 不变，将整单退回")
    return True, tip, data
