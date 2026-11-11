"""cs.address_book 地址簿 —— 顾客收货地址的保存 / 复用 / 管理。

顾客在下单弹窗里填写的地址可以保存到自己的地址簿，下次下单直接选用；
地址簿支持新增、修改、删除，并可以指定一条「默认地址」（下单时自动带出）。

数据表 cs.address_book：
    id / user_id / receiver_name / phone
    province / city / district / detail / address（省市区 + 详细地址的完整串）
    is_default 是否默认地址 / create_time / update_time

对外接口：
    ensure_address_table()                     建表
    list_addresses(user_id)                    -> [dict, ...]（默认地址在最前）
    get_address(user_id, addr_id)              -> dict | None
    create_address(user_id, data)              -> (是否成功, 提示, dict)
    update_address(user_id, addr_id, data)     -> (是否成功, 提示, dict)
    delete_address(user_id, addr_id)           -> (是否成功, 提示)
    set_default_address(user_id, addr_id)      -> (是否成功, 提示, dict)
"""
import re
import threading
import time

from 公共.db_config import get_conn
from 公共.regions import validate_region

TABLE = "`address_book`"
PHONE_RE = re.compile(r"^[0-9\-+\s]{6,20}$")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `address_book` (
    `id`            INT          NOT NULL AUTO_INCREMENT   COMMENT '自增ID',
    `user_id`       VARCHAR(64)  NOT NULL                  COMMENT '所属用户ID',
    `receiver_name` VARCHAR(64)  DEFAULT NULL              COMMENT '收货人姓名',
    `phone`         VARCHAR(32)  DEFAULT NULL              COMMENT '联系电话',
    `province`      VARCHAR(64)  DEFAULT NULL              COMMENT '省',
    `city`          VARCHAR(64)  DEFAULT NULL              COMMENT '市',
    `district`      VARCHAR(64)  DEFAULT NULL              COMMENT '区/县',
    `detail`        VARCHAR(255) DEFAULT NULL              COMMENT '详细地址',
    `address`       VARCHAR(255) DEFAULT NULL              COMMENT '完整地址（省市区+详细）',
    `is_default`    TINYINT      DEFAULT 0                 COMMENT '1=默认地址',
    `create_time`   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME     DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户地址簿';
"""

_ensure_done = False
_ensure_lock = threading.Lock()

def ensure_address_table():
    """确保 cs.address_book 存在。返回 (是否成功, 提示信息)。

    建表属于 DDL，会加元数据锁，因此进程内只执行一次。
    """
    global _ensure_done
    if _ensure_done:
        return True, "cs.address_book 表已就绪"
    with _ensure_lock:
        if _ensure_done:
            return True, "cs.address_book 表已就绪"
        try:
            conn = get_conn()
        except Exception as e:
            return False, f"数据库连接失败：{e}"
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            conn.commit()
            _ensure_done = True
            return True, "cs.address_book 表已就绪"
        except Exception as e:
            conn.rollback()
            return False, f"地址簿表初始化失败：{e}"
        finally:
            conn.close()

def _s(value):
    return str(value if value is not None else "").strip()

def normalize_address(data):
    """校验并整理一条地址。返回 (是否成功, 提示, 整理后的 dict)。

    省 / 市 / 区必须来自行政区划数据（与下单同一套校验），详细地址单独填写。
    """
    data = data or {}
    receiver_name = _s(data.get("receiver_name"))
    phone = _s(data.get("phone"))
    province = _s(data.get("province"))
    city = _s(data.get("city"))
    district = _s(data.get("district"))
    detail = _s(data.get("detail"))

    if not receiver_name:
        return False, "请填写收货人姓名", None
    if not phone:
        return False, "请填写联系电话", None
    if not PHONE_RE.match(phone):
        return False, "联系电话格式不正确", None
    ok, msg = validate_region(province, city, district)
    if not ok:
        return False, msg, None
    if not detail:
        return False, "请填写详细地址（街道、门牌号等）", None

    return True, "", {
        "receiver_name": receiver_name,
        "phone": phone,
        "province": province,
        "city": city,
        "district": district,
        "detail": detail,
        "address": f"{province}{city}{district}{detail}",
    }

def _format_row(row):
    if row is None:
        return None
    row["is_default"] = int(row.get("is_default") or 0)
    for key in ("create_time", "update_time"):
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row

def list_addresses(user_id):
    """某用户的全部地址，默认地址排在最前，其余按创建时间倒序。"""
    ok, _ = ensure_address_table()
    if not ok:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {TABLE} WHERE user_id=%s "
                "ORDER BY is_default DESC, create_time DESC",
                (str(user_id),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_format_row(r) for r in rows]

def get_address(user_id, addr_id):
    """按 ID 取一条属于该用户的地址；不存在返回 None。"""
    ok, _ = ensure_address_table()
    if not ok:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {TABLE} WHERE id=%s AND user_id=%s",
                (addr_id, str(user_id)),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return _format_row(row)

def _clear_default(cur, user_id):
    cur.execute(f"UPDATE {TABLE} SET is_default=0 WHERE user_id=%s", (str(user_id),))

def create_address(user_id, data):
    """新增一条地址；第一条自动成为默认地址，也可显式要求设为默认。"""
    ok, msg = ensure_address_table()
    if not ok:
        return False, msg, None
    ok, msg, payload = normalize_address(data)
    if not ok:
        return False, msg, None

    want_default = bool(data.get("is_default"))
    addr_id = None
    for attempt in range(3):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM {TABLE} WHERE user_id=%s",
                            (str(user_id),))
                first = int((cur.fetchone() or {}).get("c") or 0) == 0
                if first:
                    # 第一条本身就是默认地址：不必再清空旧默认。
                    want_default = True
                elif want_default:
                    _clear_default(cur, user_id)
                cur.execute(
                    f"INSERT INTO {TABLE} "
                    "(user_id, receiver_name, phone, province, city, district, detail, address, is_default) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (str(user_id), payload["receiver_name"], payload["phone"],
                     payload["province"], payload["city"], payload["district"],
                     payload["detail"], payload["address"], 1 if want_default else 0),
                )
                addr_id = cur.lastrowid
            conn.commit()
            break
        except Exception as e:
            conn.rollback()
            addr_id = None
            deadlock = bool(getattr(e, "args", None)) and e.args[0] == 1213
            if deadlock and attempt < 2:
                time.sleep(0.05 * (attempt + 1))      # 死锁重试
                continue
            return False, f"地址保存失败：{e}", None
        finally:
            conn.close()

    if addr_id is None:
        return False, "地址保存失败", None
    row = get_address(user_id, addr_id)
    return True, ("地址已保存并设为默认" if want_default else "地址已保存到地址簿"), row

def update_address(user_id, addr_id, data):
    """修改一条地址（只允许改自己的）。"""
    ok, msg = ensure_address_table()
    if not ok:
        return False, msg, None
    if get_address(user_id, addr_id) is None:
        return False, "地址不存在或无权修改", None
    ok, msg, payload = normalize_address(data)
    if not ok:
        return False, msg, None

    want_default = bool(data.get("is_default"))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if want_default:
                _clear_default(cur, user_id)
            cur.execute(
                f"UPDATE {TABLE} SET receiver_name=%s, phone=%s, province=%s, city=%s, "
                "district=%s, detail=%s, address=%s"
                + (", is_default=1" if want_default else "")
                + " WHERE id=%s AND user_id=%s",
                (payload["receiver_name"], payload["phone"], payload["province"],
                 payload["city"], payload["district"], payload["detail"],
                 payload["address"], addr_id, str(user_id)),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"地址修改失败：{e}", None
    finally:
        conn.close()

    return True, "地址已更新", get_address(user_id, addr_id)

def delete_address(user_id, addr_id):
    """删除一条地址；删掉默认地址后，剩余最新一条自动补为默认。"""
    ok, msg = ensure_address_table()
    if not ok:
        return False, msg
    row = get_address(user_id, addr_id)
    if row is None:
        return False, "地址不存在或无权删除"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE id=%s AND user_id=%s",
                        (addr_id, str(user_id)))
            if row.get("is_default"):
                cur.execute(
                    f"SELECT id FROM {TABLE} WHERE user_id=%s "
                    "ORDER BY create_time DESC LIMIT 1",
                    (str(user_id),),
                )
                nxt = cur.fetchone()
                if nxt:
                    cur.execute(f"UPDATE {TABLE} SET is_default=1 WHERE id=%s", (nxt["id"],))
        conn.commit()
        return True, "地址已删除"
    except Exception as e:
        conn.rollback()
        return False, f"地址删除失败：{e}"
    finally:
        conn.close()

def set_default_address(user_id, addr_id):
    """把某条地址设为默认（同一用户只会有一条默认地址）。"""
    ok, msg = ensure_address_table()
    if not ok:
        return False, msg, None
    if get_address(user_id, addr_id) is None:
        return False, "地址不存在或无权操作", None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _clear_default(cur, user_id)
            cur.execute(f"UPDATE {TABLE} SET is_default=1 WHERE id=%s AND user_id=%s",
                        (addr_id, str(user_id)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"设置默认地址失败：{e}", None
    finally:
        conn.close()

    return True, "已设为默认地址", get_address(user_id, addr_id)
