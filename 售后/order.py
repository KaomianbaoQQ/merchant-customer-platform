"""
cs.order 表的建表 + 增删改查。

数据来源：Dify 工作流返回的 ops 数组（main.py 打印的那份 JSON）。
每个 op 形如：
    {"operate": "insert", "insert_id": "A001", "insert_goodsname": "电脑", ...}
    {"operate": "delete", "delete_id": "A001", "delete_goodsname": null, ...}
    {"operate": "update", "update_id": "A001", "update_goodsname": "手机", ...}
    {"operate": "select", "query_id": "A001",  "query_goodsname": "电脑", ...}

注意：order 是 MySQL 保留字，SQL 中表名必须写成 `order`（带反引号）。

两张表：
    cs.order        订单数据
    cs.operate_log  操作日志：每一次 insert / update / delete 都会记一条，
                    包含被操作的订单ID、商品（update 含新旧值）、操作人与操作时间。
"""
import json
import re
import uuid
from datetime import datetime

from 公共.db_config import get_conn
from 物流.repository import ensure_logistics, create_logistics, waybill_exists
from 物流.status import compute_status, format_datetime

# order 为保留字，统一用反引号包裹
TABLE = "`order`"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `order` (
    `id`          VARCHAR(64)  NOT NULL                     COMMENT '记录ID/订单号',
    `goodsname`   VARCHAR(255) DEFAULT NULL                 COMMENT '商品名称',
    `product_id`  VARCHAR(128) DEFAULT NULL                 COMMENT '商品ID（用于销量统计）',
    `sku_id`      VARCHAR(128) DEFAULT NULL                 COMMENT 'SKU ID',
    `sku_name`    VARCHAR(255) DEFAULT NULL                 COMMENT '规格名称',
    `reservation_id` VARCHAR(64) DEFAULT NULL              COMMENT '库存预占单号',
    `pay_status`  VARCHAR(16)  DEFAULT 'unpaid'            COMMENT 'unpaid/paid/cancelled',
    `name`        VARCHAR(64)  DEFAULT NULL                 COMMENT '操作人姓名',
    `user_id`     VARCHAR(64)  DEFAULT NULL                 COMMENT '操作人ID',
    `role`        VARCHAR(32)  DEFAULT NULL                 COMMENT '操作人角色',
    `address`     VARCHAR(255) DEFAULT NULL                 COMMENT '收货地址',
    `waybill`     VARCHAR(32)  DEFAULT NULL                 COMMENT '运单号（商家发货时填写）',
    `group_id`    VARCHAR(64)  DEFAULT NULL                 COMMENT '同一次结算的分组ID',
    `quantity`    INT          DEFAULT 1                    COMMENT '数量',
    `price`       DECIMAL(10,2) DEFAULT 0                   COMMENT '单价',
    `create_time` DATETIME     DEFAULT CURRENT_TIMESTAMP    COMMENT '创建时间',
    `update_time` DATETIME     DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP  COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_waybill` (`waybill`),
    KEY `idx_product` (`product_id`),
    KEY `idx_reservation` (`reservation_id`),
    KEY `idx_user_time` (`user_id`, `create_time`),
    KEY `idx_order_status` (`order_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表';
"""

# 订单生命周期（业务状态机，由事件驱动；与物流进度 status_step 0~4 区分）：
#   pending_shipment 待发货（已下单，商家未发货）
#   shipped          待收货（已发货，用户未确认）
#   completed        已完成（用户确认收货 / 已签收）
#   cancelled        已取消（发货前用户取消）
ORDER_STATUS_TEXTS = {
    "pending_shipment": "待发货",
    "shipped": "待收货",
    "completed": "已完成",
    "cancelled": "已取消",
}

# 操作日志表：记录所有 insert / update / delete
LOG_TABLE = "`operate_log`"

CREATE_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `operate_log` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT      COMMENT '日志自增ID',
    `operate`       VARCHAR(16)  NOT NULL                     COMMENT '操作类型：insert/update/delete',
    `order_id`      VARCHAR(64)  DEFAULT NULL                 COMMENT '被操作的订单ID',
    `goodsname`     VARCHAR(255) DEFAULT NULL                 COMMENT '商品名（update 为改后新值）',
    `old_goodsname` VARCHAR(255) DEFAULT NULL                 COMMENT '商品名旧值（仅 update）',
    `operator_name` VARCHAR(64)  DEFAULT NULL                 COMMENT '操作人姓名',
    `operator_id`   VARCHAR(64)  DEFAULT NULL                 COMMENT '操作人ID',
    `operator_role` VARCHAR(32)  DEFAULT NULL                 COMMENT '操作人角色',
    `create_time`   DATETIME     DEFAULT CURRENT_TIMESTAMP    COMMENT '操作时间',
    PRIMARY KEY (`id`),
    KEY `idx_order_id` (`order_id`),
    KEY `idx_operator` (`operator_id`),
    KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单操作日志表';
"""


def _column_exists(cur, table_name, column_name):
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table_name, column_name),
    )
    return cur.fetchone() is not None


def ensure_tables():
    """确保 cs.order 与 cs.operate_log 两张表都已存在。返回 (是否成功, 提示信息)"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(CREATE_LOG_TABLE_SQL)
            # 老表补 quantity 字段
            if not _column_exists(cur, "order", "quantity"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `quantity` INT DEFAULT 1 COMMENT '数量' AFTER `waybill`"
                )
                # 把老数据 goodsname 里的 " xN" 拆到 quantity，商品名还原为纯名字
                cur.execute(f"SELECT id, goodsname FROM {TABLE}")
                for row in cur.fetchall():
                    name, qty = split_goods_qty(row.get("goodsname"))
                    if qty and qty > 0:
                        cur.execute(
                            f"UPDATE {TABLE} SET goodsname=%s, quantity=%s WHERE id=%s",
                            (name, qty, row["id"]),
                        )
            # 商品ID：用于把订单归属到具体商品，统计销量
            if not _column_exists(cur, "order", "product_id"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `product_id` VARCHAR(128) DEFAULT NULL COMMENT '商品ID' AFTER `goodsname`"
                )
                cur.execute(f"ALTER TABLE {TABLE} ADD KEY `idx_product` (`product_id`)")
            # 单价：用于统计销售额 / 退款金额
            if not _column_exists(cur, "order", "price"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `price` DECIMAL(10,2) DEFAULT 0 COMMENT '单价' AFTER `quantity`"
                )
            # 同一次结算的多件商品共用一个 group_id，便于商家合并发货
            if not _column_exists(cur, "order", "group_id"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `group_id` VARCHAR(64) DEFAULT NULL COMMENT '同一次结算的分组ID' AFTER `waybill`"
                )
            # 商家归属：混合下单时按商家拆单/各自发货/物流校验的精确依据
            if not _column_exists(cur, "order", "merchant_id"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `merchant_id` VARCHAR(64) DEFAULT NULL COMMENT '商品所属商家ID' AFTER `group_id`"
                )
                cur.execute(f"ALTER TABLE {TABLE} ADD KEY `idx_merchant` (`merchant_id`)")
            if not _column_exists(cur, "order", "merchant_name"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `merchant_name` VARCHAR(128) DEFAULT NULL COMMENT '商品所属店铺名' AFTER `merchant_id`"
                )
            # 收货人电话：下单时填写，商家发货可见
            if not _column_exists(cur, "order", "phone"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `phone` VARCHAR(32) DEFAULT NULL COMMENT '收货人电话' AFTER `address`"
                )
            # SKU 库存相关列：老表逐列补齐（CREATE TABLE IF NOT EXISTS 不会改已有表）
            new_columns = (
                ("sku_id", "ADD COLUMN `sku_id` VARCHAR(128) DEFAULT NULL COMMENT 'SKU ID'"),
                ("sku_name", "ADD COLUMN `sku_name` VARCHAR(255) DEFAULT NULL COMMENT '规格名称'"),
                ("reservation_id", "ADD COLUMN `reservation_id` VARCHAR(64) DEFAULT NULL COMMENT '库存预占单号'"),
                ("pay_status", "ADD COLUMN `pay_status` VARCHAR(16) DEFAULT 'unpaid' COMMENT '支付状态'"),
            )
            for col, ddl in new_columns:
                if not _column_exists(cur, "order", col):
                    cur.execute(f"ALTER TABLE {TABLE} {ddl}")
                    if col == "reservation_id":
                        cur.execute(f"ALTER TABLE {TABLE} ADD KEY `idx_reservation` (`reservation_id`)")
            # 订单生命周期状态：老表补列并回填（有运单→待收货，否则→待发货）
            if not _column_exists(cur, "order", "order_status"):
                cur.execute(
                    f"ALTER TABLE {TABLE} "
                    "ADD COLUMN `order_status` VARCHAR(16) DEFAULT 'pending_shipment' "
                    "COMMENT '生命周期：pending_shipment/shipped/completed/cancelled' AFTER `pay_status`"
                )
                cur.execute(f"ALTER TABLE {TABLE} ADD KEY `idx_order_status` (`order_status`)")
                cur.execute(
                    f"UPDATE {TABLE} SET order_status='shipped' WHERE waybill IS NOT NULL AND waybill <> ''"
                )
            _backfill_merchants(cur)
        conn.commit()
        return True, "cs.order / cs.operate_log 表已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"建表失败：{e}"
    finally:
        conn.close()


def _backfill_merchants(cur):
    """按 product_id 给存量订单补商家归属（商品已删除的订单保留 NULL，走名称兜底）。"""
    try:
        from 公共 import store
        products = store.list_products()
    except Exception:
        return
    pid_map = {
        str(p.get("id")): (str(p.get("owner_id") or ""), str(p.get("owner_name") or ""))
        for p in products if p.get("id")
    }
    cur.execute(
        f"SELECT id, product_id FROM {TABLE} "
        "WHERE merchant_id IS NULL AND product_id IS NOT NULL AND product_id <> ''"
    )
    for row in cur.fetchall():
        owner = pid_map.get(str(row.get("product_id")))
        if owner and owner[0]:
            cur.execute(
                f"UPDATE {TABLE} SET merchant_id=%s, merchant_name=%s WHERE id=%s",
                (owner[0], owner[1], row["id"]),
            )


def _write_log(cur, operate, order_id, goodsname, old_goodsname, operator):
    """
    写一条操作日志（复用业务操作同一个事务/游标，保证原子）。
    只在 insert / update / delete 成功时调用。
    """
    cur.execute(
        f"INSERT INTO {LOG_TABLE} "
        "(operate, order_id, goodsname, old_goodsname, operator_name, operator_id, operator_role) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (operate, order_id, goodsname, old_goodsname,
         operator.get("name"), operator.get("id"), operator.get("role")),
    )


def get_logs(limit=50):
    """查看最近的操作日志（按时间倒序）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, operate, order_id, goodsname, old_goodsname, "
                f"operator_name, operator_id, operator_role, create_time "
                f"FROM {LOG_TABLE} ORDER BY id DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()
        for r in rows:
            if r.get("create_time") is not None:
                r["create_time"] = str(r["create_time"])
        return rows
    finally:
        conn.close()


def _gen_id():
    """用户没给 insert_id 时，自动生成一个订单号。

    随机后缀用 8 位十六进制（约 43 亿种）：原来只有 4 位（65536 种），
    同一秒内并发下单几十笔就会撞主键（Duplicate entry），高并发仿真验证过。
    """
    return "ORD" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:8].upper()


def _s(value):
    """把 None / 非字符串统一转成去掉首尾空格的字符串"""
    if value is None:
        return ""
    return str(value).strip()


# 商品名末尾的 " x5" / " ×5" 数量后缀
_QTY_SUFFIX_RE = re.compile(r"\s*[xX×*]\s*(\d+)\s*$")


def split_goods_qty(goodsname):
    """把 "机械键盘 x5" 拆成 ("机械键盘", 5)；无后缀则返回 (原名字, None)。"""
    name = _s(goodsname)
    m = _QTY_SUFFIX_RE.search(name)
    if not m:
        return name, None
    return name[:m.start()].strip(), int(m.group(1))


def effective_quantity(row):
    """订单数量：优先用 quantity 字段；老数据从 goodsname 的 " xN" 后缀解析；默认 1。"""
    if not isinstance(row, dict):
        return 1
    try:
        q = int(row.get("quantity") or 0)
        if q > 0:
            return q
    except (TypeError, ValueError):
        pass
    _name, q = split_goods_qty(row.get("goodsname"))
    return q or 1


def base_goodsname(goodsname):
    """去掉数量后缀后的商品名。"""
    return split_goods_qty(goodsname)[0]


# 7 位纯数字快递单号（商家发货时填写）
WAYBILL_RE = re.compile(r"^\d{7}$")


def normalize_waybill(waybill):
    """校验并返回 7 位快递单号；不合法返回空字符串。"""
    code = _s(waybill)
    return code if WAYBILL_RE.match(code) else ""


def order_status(row):
    """订单当前的物流档位和文案。

    - 有物流记录：按发货 / 预计送达时间动态计算。
    - 真正的结算订单（有收货地址或分组ID）但还没运单 → 0 待发货。
    - 历史记录（无地址、无运单，如工作流直接 insert 的记录）→ 4 已签收。
    """
    if not isinstance(row, dict):
        return 0, "待发货"
    shipped_at = row.get("shipped_at")
    eta = row.get("estimated_arrival")
    if shipped_at and eta:
        return compute_status(shipped_at, eta)
    if row.get("waybill"):
        # 有运单但物流记录缺失：无法计算，按待发货处理，等商家重新发货
        return 0, "待发货"
    if row.get("address") or row.get("group_id"):
        return 0, "待发货"
    return 4, "已签收"


# ==================== 一、严格识别 Dify 返回的业务数据 ====================
# ID 是编号，不会含空格，可以按逗号/顿号/分号/空白拆分
_ID_SPLIT_RE = re.compile(r"[,，、;；\s]+")
# 商品名可能自带空格（如 "iPhone 15"），只按逗号/顿号/分号拆分，不能按空白拆
_GOODS_SPLIT_RE = re.compile(r"[,，、;；]+")

# 每种操作对应自己的 ID 字段与商品名字段
_ID_FIELD = {
    "insert": "insert_id",
    "delete": "delete_id",
    "update": "update_id",
    "select": "query_id",
}
_GOODS_FIELD = {
    "insert": "insert_goodsname",
    "delete": "delete_goodsname",
    "update": "update_goodsname",
    "select": "query_goodsname",
}


def _strip_code_fence(text):
    """去掉 ```json ... ``` 这类 markdown 包裹"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _split_multi(value, pattern=None):
    """把 "X1,X2,X3" / "A1、A2、A3" 拆成 ['X1','X2','X3']"""
    s = _s(value)
    if not s:
        return []
    return [p for p in (pattern or _ID_SPLIT_RE).split(s) if p]


def _expand_op(op):
    """
    一条 op 里如果写了多个 ID（"X1,X2,X3"），展开成多条单行 op，
    使每个 ID 对应 order 表中的一行。
    """
    operate = _s(op.get("operate")).lower()
    id_field = _ID_FIELD.get(operate)
    goods_field = _GOODS_FIELD.get(operate)
    if not id_field:
        return [op]

    ids = _split_multi(op.get(id_field))
    goods_list = _split_multi(op.get(goods_field), _GOODS_SPLIT_RE) if goods_field else []

    # 单个 ID、单个商品名（或都没有）→ 不需要展开
    if len(ids) <= 1 and len(goods_list) <= 1:
        return [op]

    count = max(len(ids), len(goods_list))
    expanded = []
    for i in range(count):
        new_op = dict(op)
        if len(ids) == 1:
            new_op[id_field] = ids[0]
        else:
            new_op[id_field] = ids[i] if i < len(ids) else ""

        if goods_field:
            if len(goods_list) == len(ids):
                new_op[goods_field] = goods_list[i] if i < len(goods_list) else ""
            elif len(goods_list) == 1:
                new_op[goods_field] = goods_list[0]
            else:
                new_op[goods_field] = goods_list[i] if i < len(goods_list) else ""
        expanded.append(new_op)
    return expanded


def parse_ops_payload(raw):
    """
    严格解析 Dify 返回的业务字段。

    返回 (ops_list, err_text)：
      - 解析成功且是合法 JSON 结构 → (规范化后的操作列表, None)
      - raw 为 None（本次没有业务数据） → ([], None)
      - 不是 JSON / 结构不认识 → ([], 原始文本)  ← main 负责打印这段原文

    支持的形态：
      1) dict  {"ops": [...]}        多操作
      2) dict  {"operate": ...}      单操作（Dify 的 json 字段）
      3) list  [ {...}, {...} ]       操作数组
      4) str   JSON 文本（可带 ```json 围栏）
    """
    if raw is None:
        return [], None

    obj = raw

    # 形态 4：字符串先尝试当 JSON 解析
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return [], None
        try:
            obj = json.loads(_strip_code_fence(text))
        except (ValueError, TypeError):
            return [], raw  # 非 JSON → 原文交回 main 打印

    # 提取操作数组
    if isinstance(obj, list):
        ops = obj
    elif isinstance(obj, dict):
        if isinstance(obj.get("ops"), list):
            ops = obj["ops"]
        elif "operate" in obj:
            ops = [obj]
        else:
            return [], raw  # 结构不认识 → 原文打印
    else:
        return [], raw

    # 逐条校验 + 展开"多 ID 一行"
    normalized = []
    for item in ops:
        if not isinstance(item, dict):
            return [], raw
        normalized.extend(_expand_op(item))
    return normalized, None


# ============================ 归属查询 ============================
def get_order_owner(order_id):
    """
    查询某个订单属于哪个用户，返回 user_id（字符串）；订单不存在返回 None。
    main.py 用它做"只能操作自己订单"的前置校验。
    """
    oid = _s(order_id)
    if not oid:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT user_id FROM {TABLE} WHERE id=%s", (oid,))
            row = cur.fetchone()
            return str(row["user_id"]) if row and row["user_id"] is not None else None
    finally:
        conn.close()


# ==================== 用户订单列表（含运单号/物流状态，供上传工作流 / 我的订单） ====================
def get_orders_by_user(user_id, limit=None):
    """
    查询某个用户自己的订单，LEFT JOIN cs.logistics 带出运单号与物流状态。
    按创建时间倒序（第一条最新）。limit=None 表示取全部订单。
    返回 [{"id","goodsname","address","waybill","create_time","update_time",
            "status_step","status_text","carrier"}, ...]
    """
    ensure_logistics()
    sql = (
        "SELECT o.id, o.goodsname, o.product_id, o.sku_id, o.sku_name, o.quantity, o.price, "
        "o.name, o.user_id, o.address, o.waybill, o.order_status, o.pay_status, "
        "o.group_id, o.merchant_id, o.merchant_name, o.create_time, o.update_time, "
        "l.shipped_at, l.estimated_arrival, l.carrier "
        f"FROM {TABLE} o LEFT JOIN `logistics` l ON o.waybill = l.waybill "
        "WHERE o.user_id=%s ORDER BY o.create_time DESC"
    )
    params = [str(user_id)]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            # 每单已申请的退货/换货数量
            cur.execute(
                "SELECT order_id, COALESCE(SUM(quantity), 0) AS q FROM `return_order` "
                "WHERE user_id=%s AND NOT (`type`='refund' AND `status`='rejected') "
                "GROUP BY order_id",
                (str(user_id),),
            )
            returned = {r["order_id"]: int(r["q"] or 0) for r in cur.fetchall()}
    except Exception:
        returned = {}
    finally:
        conn.close()

    for r in rows:
        step, text = order_status(r)
        r["status_step"] = step
        r["status_text"] = text
        os_ = str(r.get("order_status") or "pending_shipment")
        r["order_status"] = os_
        r["order_status_text"] = ORDER_STATUS_TEXTS.get(os_, "未知")
        qty = effective_quantity(r)
        used = returned.get(r.get("id"), 0)
        r["quantity"] = qty
        r["returned_quantity"] = used
        r["returnable_quantity"] = max(0, qty - used)
        for k in ("create_time", "update_time"):
            if r.get(k) is not None:
                r[k] = str(r[k])
        for k in ("shipped_at", "estimated_arrival"):
            if r.get(k) is not None:
                r[k] = format_datetime(r[k])
    return rows


# ==================== 用户订单文本（无论输入什么都上传全部订单） ====================
def build_user_orders(user, limit=None):
    """
    把当前用户的【全部】订单（含运单号 / 物流状态）整理成文本，随工作流入参上传。
    无论用户输入什么，售后都会带上全部订单，避免用户说"退款/退换货"时搞不清是哪一单。
    带显式序号（1 = 最新），降低 LLM 误判概率。
    """
    if user["is_admin"]:
        return "（管理员账号：可操作全部订单，未上传订单列表；请按用户给出的订单号或商品名操作）"

    orders = get_orders_by_user(user["id"], limit=limit)
    if not orders:
        return "[]（该用户当前没有任何订单）"

    items = []
    for i, o in enumerate(orders, 1):
        items.append({
            "序号": i,
            "订单号": o["id"],
            "商品": o["goodsname"],
            "下单时间": o.get("create_time"),
            "运单号": o.get("waybill"),
            "物流状态": o.get("status_text"),
        })
    text = json.dumps(items, ensure_ascii=False)
    text += "（这是该用户全部订单，已按创建时间倒序：序号 1 为最新；" \
            "用户提到退款/退货/换货时，请根据此列表确认具体订单，不要反问是哪一单）"
    return text


# ============================ 增 ============================
def insert_order(op, operator, scope_user_id=None):
    """scope_user_id 这里用不到：新增时的操作人本来就取自当前登录用户。"""
    order_id = _s(op.get("insert_id"))
    goods = _s(op.get("insert_goodsname"))

    # 至少要有一个可识别信息，否则就是空指令（如"随便新增点东西"）
    if not order_id and not goods:
        return False, "新增失败：insert_id 与 insert_goodsname 均为空，无法新增", None

    if not order_id:
        order_id = _gen_id()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {TABLE} WHERE id=%s", (order_id,))
            if cur.fetchone():
                return False, f"新增失败：记录ID {order_id} 已存在", None
            cur.execute(
                f"INSERT INTO {TABLE} (id, goodsname, name, user_id, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                (order_id, goods, operator.get("name"),
                 operator.get("id"), operator.get("role")),
            )
            _write_log(cur, "insert", order_id, goods, None, operator)
        conn.commit()
        data = {"id": order_id, "goodsname": goods,
                "name": operator.get("name"), "user_id": operator.get("id"),
                "role": operator.get("role")}
        return True, f"新增成功：ID={order_id}，商品={goods or '(空)'}", data
    except Exception as e:
        conn.rollback()
        return False, f"新增失败：{e}", None
    finally:
        conn.close()


# ==================== 下单（电商结算写入 order，带地址） ====================
def create_checkout_orders(items, operator, address, waybill=None, phone=None,
                           receiver_name=None, group_id=None, lines=None,
                           reservation_id=None):
    """
    电商「去结算」时把一个购物袋拆成多条 order 记录写入 cs.order。

    items: [{"name": "笔记本电脑", "qty": 1, "price": 5299, "product_id": "...", "sku_id": "..."}, ...]
           name/price/sku_id 仅作回显参考，**以下单时商品库的权威数据为准**（防止前端改价）。
    operator: {"name": "张三", "id": "1", "role": "user"}
    address:  收货地址（写入 order.address）
    waybill:  运单号（前台下单留空，由商家发货时填写）
    phone:    收货人电话（写入 order.phone）
    receiver_name: 收货人姓名（不传则用 operator.name）
    lines:    已由 公共/inventory.py 校验并合并的下单项（按 SKU 拆分），不传则内部自行规范化
    group_id / reservation_id: 一次结算的分组号与库存预占单号

    同一次结算的订单共用一个 group_id；每个 SKU 各自一条订单，
    商家在后台只能看到并发本店商品，实现混合下单、分商家发货。
    返回 (是否成功, 提示, [订单ID, ...])
    """
    ok, msg = ensure_tables()
    if not ok:
        return False, msg, []

    items = items or []
    if not items:
        return False, "下单失败：购物袋为空", []
    if len(address or "") > 255:
        return False, "收货地址过长（最多 255 字）", []

    # 服务端按 product_id / sku_id 重新取权威商品与规格数据，防止前端篡改名称、价格、规格。
    # lines 由库存预占阶段传入（已校验并合并同一 SKU）；未传时自行规范化，兼容旧调用。
    if lines is None:
        from 公共 import inventory
        ok, msg, lines = inventory.normalize_order_items(items)
        if not ok:
            return False, msg, []
    if not lines:
        return False, "下单失败：购物袋为空", []

    # 真实预占库存：本次结算未携带预占单号时，按 SKU 原子预占（防超卖）。
    # 预占失败（某 SKU 可售不足）直接拦截下单，并给出具体 SKU 与可用量。
    if reservation_id is None:
        from 公共 import inventory
        ok_res, msg_res, rid, _ = inventory.reserve(
            items, user_id=operator.get("id"),
        )
        if not ok_res:
            return False, msg_res, []
        reservation_id = rid

    group_id = group_id or ("G" + uuid.uuid4().hex[:12])
    order_ids = []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for line in lines:
                product = line["product"]
                qty = min(int(line["qty"]), 999)
                goods = line["name"]
                price = float(line["price"] or 0)
                pid = line["product_id"]
                merchant_id = str(product.get("owner_id") or "")
                merchant_name = str(product.get("owner_name") or "")

                order_id = _gen_id()
                rname = _s(receiver_name) or _s(operator.get("name"))
                cur.execute(
                    f"INSERT INTO {TABLE} "
                    "(id, goodsname, product_id, sku_id, sku_name, name, user_id, role, "
                    "address, phone, waybill, group_id, reservation_id, pay_status, "
                    "merchant_id, merchant_name, quantity, price) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s)",
                    (order_id, goods, pid, line["sku_id"], line["sku_name"], rname,
                     operator.get("id"), operator.get("role"), address, phone, waybill,
                     group_id, reservation_id, "unpaid",
                     merchant_id or None, merchant_name or None, qty, price),
                )
                _write_log(cur, "insert", order_id, goods, None, operator)
                order_ids.append(order_id)
        conn.commit()
        total_qty = sum(int(x["qty"]) for x in lines)
        shop_count = len({str(x["product"].get("owner_id") or "") for x in lines})
        if shop_count > 1:
            tip = f"下单成功：共 {total_qty} 件商品，将由 {shop_count} 家店铺分别发货"
        else:
            tip = f"下单成功：共 {total_qty} 件商品，等待商家发货"
        return True, tip, order_ids
    except Exception as e:
        conn.rollback()
        return False, f"下单失败：{e}", []
    finally:
        conn.close()


def set_pay_status(reservation_id, status):
    """按库存预占单号批量更新一次结算的订单支付状态。"""
    rid = _s(reservation_id)
    if not rid or status not in ("paid", "cancelled", "unpaid"):
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE} SET pay_status=%s WHERE reservation_id=%s",
                (status, rid),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def pay_by_order(order_id, user_id=None):
    """模拟支付成功：把该订单所在结算组（同 reservation_id）置为已支付，
    并确认库存预占（locked→sold）。不接真网关，仅作演示。

    返回 (是否成功, 提示)。
    """
    oid = _s(order_id)
    if not oid:
        return False, "缺少订单号"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, reservation_id, user_id, pay_status FROM {TABLE} WHERE id=%s",
                (oid,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False, "订单不存在"
    rid = row.get("reservation_id")
    if not rid:
        return False, "该订单无库存预占记录，无法完成支付"
    if user_id and str(row.get("user_id") or "") != str(user_id):
        return False, "无权支付他人的订单"
    if str(row.get("pay_status") or "") == "paid":
        return True, "该订单已支付，无需重复支付"
    if not set_pay_status(rid, "paid"):
        return False, "支付状态更新失败"
    from 公共 import inventory
    ok_c, msg_c = inventory.confirm(rid)
    if not ok_c:
        # 预占已失效（如超时释放），回滚支付状态并提示重新下单
        set_pay_status(rid, "unpaid")
        return False, "库存预占已失效（可能已超时），请重新下单：" + str(msg_c)
    return True, "支付成功，库存已扣减"


# ============================ 删 ============================
def delete_order(op, operator, scope_user_id=None):
    order_id = _s(op.get("delete_id"))
    goods = _s(op.get("delete_goodsname"))

    if not order_id and not goods:
        return False, "删除失败：delete_id 与 delete_goodsname 均为空，无法定位记录", None

    where, params = [], []
    if order_id:
        where.append("id=%s")
        params.append(order_id)
    if goods:
        where.append("goodsname=%s")
        params.append(goods)
    # 非管理员：只能删自己的订单（SQL 层面兜底）
    if scope_user_id is not None:
        where.append("user_id=%s")
        params.append(scope_user_id)
    cond = " AND ".join(where)
    desc = "，".join(filter(None, [
        f"ID={order_id}" if order_id else "",
        f"商品={goods}" if goods else "",
        "仅限本人订单" if scope_user_id is not None else "",
    ]))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, goodsname FROM {TABLE} WHERE {cond}", params)
            rows = cur.fetchall()
            if not rows:
                return False, f"删除失败：未找到匹配记录（{desc}）", None
            cur.execute(f"DELETE FROM {TABLE} WHERE {cond}", params)
            for row in rows:
                _write_log(cur, "delete", row.get("id"), row.get("goodsname"), None, operator)
        conn.commit()
        return True, f"删除成功：共删除 {len(rows)} 条记录（{desc}）", rows
    except Exception as e:
        conn.rollback()
        return False, f"删除失败：{e}", None
    finally:
        conn.close()


# ============================ 改 ============================
def update_order(op, operator, scope_user_id=None):
    order_id = _s(op.get("update_id"))
    goods = _s(op.get("update_goodsname"))

    if not order_id:
        return False, "更新失败：缺少记录ID(update_id)", None
    if not goods:
        return False, "更新失败：缺少新商品名称(update_goodsname)", None

    # 非管理员：只能改自己的订单（SQL 层面兜底）
    where = "id=%s" + (" AND user_id=%s" if scope_user_id is not None else "")
    find_params = [order_id] + ([scope_user_id] if scope_user_id is not None else [])

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, goodsname FROM {TABLE} WHERE {where}", find_params)
            row = cur.fetchone()
            if not row:
                extra = "（或不属于当前用户）" if scope_user_id is not None else ""
                return False, f"更新失败：记录ID {order_id} 不存在{extra}", None
            cur.execute(
                f"UPDATE {TABLE} SET goodsname=%s WHERE {where}",
                (goods,) + tuple(find_params),
            )
            _write_log(cur, "update", order_id, goods, row.get("goodsname"), operator)
        conn.commit()
        data = {"id": order_id, "旧商品": row.get("goodsname"), "新商品": goods}
        return True, f"更新成功：ID={order_id}，{row.get('goodsname')} → {goods}", data
    except Exception as e:
        conn.rollback()
        return False, f"更新失败：{e}", None
    finally:
        conn.close()


# ============================ 查 ============================
def select_order(op, operator, scope_user_id=None):
    order_id = _s(op.get("query_id"))
    goods = _s(op.get("query_goodsname"))

    where, params = [], []
    if order_id:
        where.append("o.id=%s")
        params.append(order_id)
    if goods:
        where.append("o.goodsname LIKE %s")
        params.append(f"%{goods}%")
    # 非管理员：只能看自己的订单
    if scope_user_id is not None:
        where.append("o.user_id=%s")
        params.append(scope_user_id)
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    ensure_logistics()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT o.id, o.goodsname, o.quantity, o.name, o.user_id, o.role, o.address, o.waybill, "
                f"o.create_time, o.update_time, l.shipped_at, l.estimated_arrival, l.carrier "
                f"FROM {TABLE} o LEFT JOIN `logistics` l ON o.waybill = l.waybill"
                f"{cond} ORDER BY o.create_time DESC LIMIT 100",
                params,
            )
            rows = cur.fetchall()
        for r in rows:
            r["status_step"], r["status_text"] = order_status(r)
            r["quantity"] = effective_quantity(r)
            for k in ("create_time", "update_time"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
            for k in ("shipped_at", "estimated_arrival"):
                if r.get(k) is not None:
                    r[k] = format_datetime(r[k])
        desc = "，".join(filter(None, [
            f"ID={order_id}" if order_id else "",
            f"商品包含\"{goods}\"" if goods else "",
            "仅限本人订单" if scope_user_id is not None else "",
        ])) or "全部记录"
        if not rows:
            return True, f"查询完成：无匹配记录（{desc}）", []
        tip = "（最多返回100条）" if len(rows) >= 100 else ""
        return True, f"查询成功：共 {len(rows)} 条记录{tip}（{desc}）", rows
    except Exception as e:
        return False, f"查询失败：{e}", None
    finally:
        conn.close()


# ============================ 统一分发 ============================
OPERATIONS = {
    "insert": insert_order,
    "delete": delete_order,
    "update": update_order,
    "select": select_order,
}


def execute_ops(ops_list, user_name="", user_id="", user_role=""):
    """
    按序执行 Dify 返回的 ops 数组，返回结果列表：
    [{"index": 1, "operate": "insert", "success": True, "message": "...", "data": {...}}, ...]

    权限：user_role 不是 admin 时，scope_user_id=当前用户ID，
    增删改查都会被限制在"自己的订单"范围内（SQL 层面兜底）。
    """
    results = []
    if not ops_list:
        return results

    ok, msg = ensure_tables()
    if not ok:
        return [{"index": 0, "operate": "-", "success": False, "message": msg, "data": None}]

    operator = {"name": user_name, "id": user_id, "role": user_role}
    scope_user_id = None if _s(user_role).lower() == "admin" else _s(user_id)

    for idx, op in enumerate(ops_list, 1):
        operate = _s(op.get("operate")).lower() if isinstance(op, dict) else ""
        func = OPERATIONS.get(operate)
        if func is None:
            results.append({
                "index": idx, "operate": operate or "(空)", "success": False,
                "message": f"不支持的操作类型：{operate or '(空)'}，仅支持 insert/delete/update/select",
                "data": None,
            })
            continue
        try:
            success, message, data = func(op, operator, scope_user_id)
        except Exception as e:
            success, message, data = False, f"执行异常：{e}", None
        results.append({
            "index": idx, "operate": operate,
            "success": success, "message": message, "data": data,
        })

    return results


# ==================== 商家销量统计（按商品聚合订单） ====================
def product_order_stats(products):
    """按商品汇总订单，供商户后台展示「浏览量 / 销量 / 销售额 / 买家」。

    参数 products: 商品 dict 列表（需含 id / name）。
    返回 {product_id: {
        "sales": 销量件数, "sales_amount": 销售额, "order_count": 订单数,
        "buyer_count": 买家数, "orders": [订单明细...]
    }} 
    订单通过 product_id 归属；老订单没有 product_id 时按商品名兜底匹配。
    """
    ensure_logistics()
    id_set = {str(p.get("id")) for p in products if p.get("id")}
    name_to_id = {}
    for p in products:
        if p.get("id"):
            name_to_id[base_goodsname(p.get("name"))] = str(p.get("id"))

    stats = {
        pid: {"sales": 0, "sales_amount": 0.0, "order_count": 0,
              "buyer_count": 0, "orders": [], "_buyers": set()}
        for pid in id_set
    }

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.goodsname, o.product_id, o.quantity, o.price, o.name, o.user_id, "
                "o.address, o.waybill, o.create_time, "
                "l.shipped_at, l.estimated_arrival, l.carrier "
                f"FROM {TABLE} o LEFT JOIN `logistics` l ON o.waybill = l.waybill "
                "ORDER BY o.create_time DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for r in rows:
        pid = str(r.get("product_id") or "")
        if pid not in stats:
            pid = name_to_id.get(base_goodsname(r.get("goodsname")))
        if pid not in stats:
            continue
        qty = effective_quantity(r)
        try:
            price = float(r.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        step, text = order_status(r)
        entry = {
            "order_id": r.get("id"),
            "goodsname": r.get("goodsname"),
            "quantity": qty,
            "price": price,
            "amount": round(price * qty, 2),
            "user_id": str(r.get("user_id") or ""),
            "user_name": r.get("name") or "",
            "address": r.get("address") or "",
            "waybill": r.get("waybill") or "",
            "status_step": step,
            "status_text": text,
            "create_time": str(r.get("create_time") or ""),
        }
        item = stats[pid]
        item["sales"] += qty
        item["sales_amount"] += entry["amount"]
        item["order_count"] += 1
        if entry["user_id"]:
            item["_buyers"].add(entry["user_id"])
        item["orders"].append(entry)

    for item in stats.values():
        item["buyer_count"] = len(item["_buyers"])
        item["sales_amount"] = round(item["sales_amount"], 2)
        del item["_buyers"]
    return stats


# ==================== 商家订单管理（查看全部订单 / 发货） ====================
def _order_owned_by(order, product_ids, product_names, strict=False, merchant_id=None):
    """订单是否属于该商户。

    优先级：
        1) 订单记录了 merchant_id（新订单）→ 必须与当前商家一致（最精确）；
        2) 老订单没有 merchant_id → 按 product_id 匹配；
        3) 再老的订单没有 product_id → 按商品名兜底。
    strict=True（发货鉴权）时：订单已有 product_id 就必须匹配该商户商品，
    避免不同商户商品重名时误操作别人的订单。
    """
    mid = str(order.get("merchant_id") or "")
    if mid and merchant_id:
        return mid == str(merchant_id)
    pid = str(order.get("product_id") or "")
    if pid:
        if pid in product_ids:
            return True
        return (not strict) and (base_goodsname(order.get("goodsname")) in product_names)
    return base_goodsname(order.get("goodsname")) in product_names


def _order_entry(r):
    """把一行订单 + 物流整理成商家 / 聊天界面可用的字典。"""
    qty = effective_quantity(r)
    try:
        price = float(r.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    step, text = order_status(r)
    return {
        "order_id": r.get("id"),
        "goodsname": r.get("goodsname"),
        "product_id": r.get("product_id") or "",
        "quantity": qty,
        "price": price,
        "amount": round(price * qty, 2),
        "user_id": str(r.get("user_id") or ""),
        "user_name": r.get("name") or "",
        "phone": r.get("phone") or "",
        "address": r.get("address") or "",
        "waybill": r.get("waybill") or "",
        "group_id": r.get("group_id") or "",
        "merchant_id": str(r.get("merchant_id") or ""),
        "merchant_name": r.get("merchant_name") or "",
        "status_step": step,
        "status_text": text,
        "create_time": str(r.get("create_time") or ""),
    }


def _query_orders(user_id=None, limit=1000, where_extra="", params_extra=None):
    """查询订单（LEFT JOIN 物流）。where_extra 为额外 SQL 条件（以 AND 开头）。"""
    ensure_logistics()
    sql = (
        "SELECT o.id, o.goodsname, o.product_id, o.quantity, o.price, o.name, o.user_id, "
        "o.address, o.phone, o.waybill, o.group_id, o.merchant_id, o.merchant_name, o.create_time, "
        "l.shipped_at, l.estimated_arrival, l.carrier "
        f"FROM {TABLE} o LEFT JOIN `logistics` l ON o.waybill = l.waybill "
    )
    cond, params = [], []
    if user_id not in (None, ""):
        cond.append("o.user_id=%s")
        params.append(str(user_id))
    if where_extra:
        cond.append("(" + where_extra.strip() + ")")
        # 参数必须跟着条件一起按同样顺序追加，否则占位符与实参错位、条件全部匹配不上
        params.extend(params_extra or [])
    if cond:
        sql += "WHERE " + " AND ".join(cond) + " "
    sql += "ORDER BY o.create_time DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def list_orders_for_owner(product_ids, product_names, user_id=None, status=None,
                          limit=1000, merchant_id=None):
    """商户订单列表：只包含该商户商品的订单，可按顾客 / 发货状态过滤。

    新订单按 merchant_id 精确过滤；老订单（merchant_id 为空）按 product_id /
    商品名兜底。status: None=全部 / "pending"=待发货 / "shipped"=已发货。
    """
    ids = {str(x) for x in (product_ids or []) if str(x)}
    names = {base_goodsname(n) for n in (product_names or []) if n}
    mid = str(merchant_id or "")

    where_extra = ""
    params_extra = []
    if mid:
        # 新订单：按 merchant_id 精确匹配；
        # 老订单（merchant_id 为空）：product_id 或商品名仍属于本店
        legacy_match = []
        legacy_params = []
        if ids:
            legacy_match.append("o.product_id IN (%s)" % ",".join(["%s"] * len(ids)))
            legacy_params.extend(ids)
        if names:
            legacy_match.append("o.goodsname IN (%s)" % ",".join(["%s"] * len(names)))
            legacy_params.extend(names)
        if legacy_match:
            where_extra = ("(o.merchant_id=%s OR (o.merchant_id IS NULL AND ("
                           + " OR ".join(legacy_match) + ")))")
            params_extra = [mid] + legacy_params
        else:
            where_extra = "o.merchant_id=%s"
            params_extra = [mid]

    rows = _query_orders(user_id=user_id, limit=limit,
                         where_extra=where_extra, params_extra=params_extra)
    result = []
    for r in rows:
        if not _order_owned_by(r, ids, names, merchant_id=mid or None):
            continue
        entry = _order_entry(r)
        if status == "pending" and entry["status_step"] != 0:
            continue
        if status == "shipped" and entry["status_step"] == 0:
            continue
        result.append(entry)

    # 标记「已申请售后、暂不可发货」的订单，供商家界面置灰发货按钮
    from .returns import open_returns_by_order, ship_block_message
    opens = open_returns_by_order([e["order_id"] for e in result])
    for e in result:
        blk = opens.get(str(e["order_id"]))
        pending_ship = (int(e["status_step"]) == 0)
        e["open_return_id"] = blk.get("id") if blk else ""
        e["open_return_status"] = blk.get("status_text") if blk else ""
        # 只有「本该能发货」的订单才提示阻止原因；已发货的订单不再提发货限制，避免误导
        e["ship_blocked_reason"] = ship_block_message(blk) if (blk and pending_ship) else ""
        e["can_ship"] = pending_ship and not blk

    # 「待发货」筛选只列还需要发货的订单：退货 / 退款已完成、无需再发货的
    # （can_ship=False）不再出现，与商户界面红点的计数口径一致。
    if status == "pending":
        result = [e for e in result if e.get("can_ship")]
    return result


def orders_belong_to_merchant(order_ids, merchant_id, product_ids, product_names):
    """给定一组订单ID，返回其中确实属于该商家的订单ID集合（用于物流物权校验）。"""
    ids_list = [str(x) for x in (order_ids or []) if str(x)]
    if not ids_list:
        return set()
    mid = str(merchant_id or "")
    ids = {str(x) for x in (product_ids or []) if str(x)}
    names = {base_goodsname(n) for n in (product_names or []) if n}
    ensure_logistics()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(ids_list))
            cur.execute(
                f"SELECT id, product_id, goodsname, merchant_id FROM {TABLE} WHERE id IN ({ph})",
                ids_list,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    owned = set()
    for r in rows:
        if _order_owned_by(r, ids, names, strict=True, merchant_id=mid or None):
            owned.add(str(r["id"]))
    return owned


def get_order_detail(order_id):
    """按订单号查询单条订单（含物流），不存在返回 None。"""
    ensure_logistics()
    oid = _s(order_id)
    if not oid:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.goodsname, o.product_id, o.quantity, o.price, o.name, o.user_id, "
                "o.address, o.waybill, o.group_id, o.merchant_id, o.merchant_name, o.create_time, "
                "l.shipped_at, l.estimated_arrival, l.carrier "
                f"FROM {TABLE} o LEFT JOIN `logistics` l ON o.waybill = l.waybill "
                "WHERE o.id=%s",
                (oid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def ship_orders(order_id, waybill, product_ids, product_names, delay_second=None, merchant_id=None):
    """商家发货：填写 7 位快递单号，把同一次结算中属于该商户的待发货订单一起发出。

    混合下单（一次结算含多个商家）时，只发出属于当前商家的订单，
    其他商家的订单保持待发货、由各自商家分别填单发货。

    返回 (是否成功, 提示信息, 物流 dict 或 None)。
    """
    code = normalize_waybill(waybill)
    if not code:
        return False, "快递单号必须是 7 位数字", None

    order = get_order_detail(order_id)
    if order is None:
        return False, "订单不存在", None

    ids = {str(x) for x in (product_ids or []) if str(x)}
    names = {base_goodsname(n) for n in (product_names or []) if n}
    mid = str(merchant_id or "")
    if not _order_owned_by(order, ids, names, strict=True, merchant_id=mid or None):
        return False, "无权操作该订单", None
    if order.get("waybill"):
        return False, f"该订单已发货（运单号 {order['waybill']}）", None
    if waybill_exists(code):
        return False, f"快递单号 {code} 已被使用，请更换", None

    # 买家已为这笔订单申请售后且尚未了结 → 不允许发货
    # （未签收只能整单退回、原运单号不变；先发货会把售后流程搞乱）
    from .returns import find_blocking_return, open_returns_by_order, ship_block_message
    blocking = find_blocking_return(order_id)
    if blocking:
        return False, ship_block_message(blocking), None

    # 同一次结算（group_id）中属于该商户、且尚未发货的订单一起发出
    targets = [order]
    group_id = _s(order.get("group_id"))
    if group_id:
        candidates = []
        for r in _query_orders(limit=None, where_extra="o.group_id=%s", params_extra=[group_id]):
            if r.get("id") == order.get("id") or r.get("waybill"):
                continue
            if _order_owned_by(r, ids, names, merchant_id=mid or None):
                candidates.append(r)
        # 同组里也有申请了售后的订单时，跳过它们，只发可以发的
        blocked = open_returns_by_order([r.get("id") for r in candidates])
        skipped = [r.get("id") for r in candidates if str(r.get("id")) in blocked]
        targets += [r for r in candidates if str(r.get("id")) not in blocked]
    else:
        skipped = []

    target_ids = [r.get("id") for r in targets]
    ok, msg, logistics = create_logistics(
        target_ids, order.get("user_id"), order.get("name"), order.get("address"),
        delay_second=delay_second, waybill=code,
    )
    if not ok:
        return False, msg, None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(target_ids))
            cur.execute(
                f"UPDATE {TABLE} SET waybill=%s, order_status='shipped' WHERE id IN ({placeholders})",
                [code] + target_ids,
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"订单发货失败：{e}", None
    finally:
        conn.close()

    tip = f"已发货：共 {len(target_ids)} 件商品，运单号 {code}"
    if skipped:
        tip += f"；另有 {len(skipped)} 笔因已申请售后未发出（{', '.join(skipped)}）"
    return True, tip, logistics


def confirm_receipt(order_id, user_id):
    """用户确认收货：仅当订单为「待收货(shipped)」时置为「已完成(completed)」。

    返回 (是否成功, 提示, 新状态)。前置：订单必须归属该用户。
    """
    oid = _s(order_id)
    uid = _s(user_id)
    if not oid or not uid:
        return False, "参数缺失", None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, user_id, order_status, waybill FROM {TABLE} WHERE id=%s", (oid,)
            )
            row = cur.fetchone()
            if not row:
                return False, "订单不存在", None
            if str(row.get("user_id")) != uid:
                return False, "只能确认自己的订单", None
            status = str(row.get("order_status") or "pending_shipment")
            if status == "completed":
                return True, "该订单已确认收货", "completed"
            if status == "cancelled":
                return False, "订单已取消，无法确认收货", status
            if status != "shipped":
                return False, "商家尚未发货，暂不能确认收货", status
            cur.execute(
                f"UPDATE {TABLE} SET order_status='completed' WHERE id=%s", (oid,)
            )
        conn.commit()
        return True, "已确认收货，订单完成", "completed"
    except Exception as e:
        conn.rollback()
        return False, f"确认收货失败：{e}", None
    finally:
        conn.close()


def cancel_order(order_id, user_id):
    """发货前取消订单：仅当「待发货(pending_shipment)」时可取消。

    返回 (是否成功, 提示, 新状态)。
    """
    oid = _s(order_id)
    uid = _s(user_id)
    if not oid or not uid:
        return False, "参数缺失", None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, user_id, order_status, reservation_id, pay_status, "
                f"sku_id, quantity, price, goodsname, product_id, merchant_id, "
                f"merchant_name FROM {TABLE} WHERE id=%s", (oid,)
            )
            row = cur.fetchone()
            if not row:
                return False, "订单不存在", None
            if str(row.get("user_id")) != uid:
                return False, "只能取消自己的订单", None
            status = str(row.get("order_status") or "pending_shipment")
            if status == "cancelled":
                return True, "该订单已取消", "cancelled"
            if status != "pending_shipment":
                return False, "订单已发货或已完成，无法取消", status
            # 取消时按支付状态分别处理库存：
            #   未支付 —— 预占还在（status=reserved），直接释放，货回到可售；
            #   已支付 —— 支付时预占已 confirm（stock 已扣、sold 已加），
            #             release 会因状态不是 reserved 失败，此时必须走
            #             「退款流水 + 库存回补」，否则订单取消了货却白扣。
            rid = _s(row.get("reservation_id"))
            paid = str(row.get("pay_status") or "") == "paid"
            refunded = False
            if rid:
                from 公共 import inventory
                try:
                    ok_rel, _msg_rel = inventory.release(rid, reason="用户取消订单")
                except Exception:
                    ok_rel = False
                if not ok_rel and paid:
                    try:
                        qty = int(row.get("quantity") or 0)
                    except (TypeError, ValueError):
                        qty = 0
                    sku_id = _s(row.get("sku_id"))
                    if sku_id and qty > 0:
                        inventory.restock(sku_id, qty, reason=f"订单取消退款({oid})")
                    # 退款流水（幂等：同一订单只记一条取消退款）
                    lid = "CANCEL:" + oid
                    cur.execute(
                        "SELECT id FROM `refund_ledger` WHERE return_id=%s", (lid,)
                    )
                    if not cur.fetchone():
                        try:
                            amount = round(float(row.get("price") or 0) * qty, 2)
                        except (TypeError, ValueError):
                            amount = 0.0
                        cur.execute(
                            "INSERT INTO `refund_ledger` "
                            "(id, return_id, order_id, user_id, user_name, owner_id, "
                            "type, amount, channel, status, remark) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 'refund', %s, "
                            "'原路退回(模拟)', 'done', %s)",
                            ("RF" + datetime.now().strftime("%Y%m%d%H%M%S")
                             + uuid.uuid4().hex[:8].upper(),
                             lid, oid, _s(row.get("user_id")), uid,
                             _s(row.get("merchant_id")), amount,
                             f"未发货取消订单退款：{row.get('goodsname') or ''}"),
                        )
                    refunded = True
            cur.execute(
                f"UPDATE {TABLE} SET order_status='cancelled', "
                f"pay_status=IF(pay_status='paid','cancelled',pay_status) WHERE id=%s",
                (oid,),
            )
        conn.commit()
        return True, ("订单已取消，款项原路退回" if refunded else "订单已取消"), "cancelled"
    except Exception as e:
        conn.rollback()
        return False, f"取消订单失败：{e}", None
    finally:
        conn.close()
