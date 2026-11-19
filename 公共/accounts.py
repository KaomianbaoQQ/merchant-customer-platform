"""商户账号：注册 / 登录 / 令牌校验（MySQL cs.merchant 表）。

支持多个商户：每个商户用「用户名 + 密码」注册登录，
登录后拿到 token，后续商品 / 会话接口都按 token 识别商户，
商品数据里也会记录 owner_id / owner_name，实现「谁上架谁管理」。

客户后端与商户后端共用同一套账号逻辑（都直接读写 cs.merchant）。
仅使用标准库 + PyMySQL。
"""
import hashlib
import re
import secrets

from 公共.db_config import get_conn

USERNAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fa5]{3,32}$")
# 密码规则：仅允许字母与数字，至少 6 位，不允许特殊字符
PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{6,}$")


def validate_password(password, password2=None):
    """校验密码格式；若传入 password2 则同时校验两次输入一致。

    返回 (是否合法, 提示信息)。
    """
    password = str(password or "")
    if not password:
        return False, "请填写密码"
    if not PASSWORD_RE.match(password):
        return False, "密码至少 6 位，且只能包含字母和数字（不允许特殊字符）"
    if password2 is not None and str(password2) != password:
        return False, "两次输入的密码不一致"
    return True, ""

CREATE_MERCHANT_SQL = """
CREATE TABLE IF NOT EXISTS `merchant` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT      COMMENT '商户ID',
    `username`    VARCHAR(64)  NOT NULL                     COMMENT '登录用户名',
    `password`    VARCHAR(128) NOT NULL                     COMMENT '密码哈希',
    `name`        VARCHAR(64)  NOT NULL                     COMMENT '联系人姓名',
    `shop_name`   VARCHAR(128) DEFAULT NULL                 COMMENT '店铺名称',
    `token`       VARCHAR(64)  DEFAULT NULL                 COMMENT '当前登录令牌',
    `create_time` DATETIME     DEFAULT CURRENT_TIMESTAMP    COMMENT '注册时间',
    `status`      VARCHAR(16)  NOT NULL DEFAULT 'approved' COMMENT '审核状态 pending/approved/rejected',
    `reviewed_by` VARCHAR(64)  DEFAULT NULL                 COMMENT '审核人',
    `reviewed_at` DATETIME     DEFAULT NULL                 COMMENT '审核时间',
    `review_note` VARCHAR(255) DEFAULT NULL                 COMMENT '审核备注',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商户账号表';
"""


def _hash_password(password):
    # 演示用途：加盐 SHA-256；生产建议换成 bcrypt / argon2
    return hashlib.sha256(("testmall$" + str(password)).encode("utf-8")).hexdigest()


REVIEW_COLUMNS = (
    ("status", "ALTER TABLE `merchant` ADD COLUMN `status` VARCHAR(16) "
               "NOT NULL DEFAULT 'approved' COMMENT '审核状态 pending/approved/rejected'"),
    ("reviewed_by", "ALTER TABLE `merchant` ADD COLUMN `reviewed_by` VARCHAR(64) "
                    "DEFAULT NULL COMMENT '审核人'"),
    ("reviewed_at", "ALTER TABLE `merchant` ADD COLUMN `reviewed_at` DATETIME "
                    "DEFAULT NULL COMMENT '审核时间'"),
    ("review_note", "ALTER TABLE `merchant` ADD COLUMN `review_note` VARCHAR(255) "
                    "DEFAULT NULL COMMENT '审核备注'"),
)


def _ensure_review_columns(cur):
    """老库平滑升级：补审核字段，已有账号一律视为审核通过，避免升级后被锁死。

    列默认值用 approved 是刻意的：直接 INSERT（演示账号脚本）与历史数据都应可用；
    只有走注册接口的新商家才显式写入 pending（见 register_merchant）。
    """
    cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='merchant'"
    )
    have = {r["COLUMN_NAME"] for r in cur.fetchall()}
    for col, sql in REVIEW_COLUMNS:
        if col not in have:
            cur.execute(sql)
    cur.execute("UPDATE `merchant` SET `status`='approved' "
                "WHERE `status` IS NULL OR `status`=''")


def ensure_merchant_table():
    """确保 cs.merchant 表存在。返回 (是否成功, 提示)。"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_MERCHANT_SQL)
            _ensure_review_columns(cur)
        conn.commit()
        return True, "merchant 表已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"建表失败：{e}"
    finally:
        conn.close()


def _row_to_info(row):
    if not row:
        return None
    mid = str(row["id"])
    from 公共.platform_state import merchant_state, get_balance
    flags = merchant_state(mid)
    bal = get_balance(mid)
    return {
        "id": mid,
        "username": row["username"],
        "name": row["name"],
        "shop_name": row.get("shop_name") or "",
        "disabled": flags["disabled"],
        "muted": flags["muted"],
        "muted_until": flags.get("muted_until") or "",
        "mute_remaining_days": flags.get("mute_remaining_days") or 0,
        "balance": bal["balance"],
        "total_revenue": bal["total_revenue"],
        "total_fine": bal["total_fine"],
        "status": row.get("status") or "approved",
        "reviewed_by": row.get("reviewed_by") or "",
        "reviewed_at": str(row.get("reviewed_at") or ""),
        "review_note": row.get("review_note") or "",
    }


def register_merchant(username, password, name, shop_name="", password2=None):
    """注册商户。返回 (是否成功, 商户信息或错误提示, 提示信息)。"""
    username = str(username or "").strip()
    password = str(password or "")
    name = str(name or "").strip()
    shop_name = str(shop_name or "").strip()

    if not USERNAME_RE.match(username):
        return False, None, "用户名需为 3-32 位中英文、数字或下划线"
    ok, msg = validate_password(password, password2)
    if not ok:
        return False, None, msg
    if not name:
        return False, None, "请填写联系人姓名"

    ok, _msg = ensure_merchant_table()
    if not ok:
        return False, None, _msg

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM `merchant` WHERE username=%s", (username,))
            if cur.fetchone():
                return False, None, "该用户名已被注册"
            cur.execute(
                "INSERT INTO `merchant` (username, password, name, shop_name, status) "
                "VALUES (%s, %s, %s, %s, 'pending')",
                (username, _hash_password(password), name, shop_name or name),
            )
            mid = cur.lastrowid
        conn.commit()
        return True, {
            "id": str(mid), "username": username, "name": name,
            "shop_name": shop_name or name, "status": "pending",
        }, "注册申请已提交，请等待平台客服审核通过后登录"
    except Exception as e:
        conn.rollback()
        return False, None, f"注册失败：{e}"
    finally:
        conn.close()


def verify_merchant(username, password):
    """校验用户名 + 密码，成功则生成新 token 并返回商户信息。"""
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return False, None, "请输入用户名和密码"

    ok, _msg = ensure_merchant_table()
    if not ok:
        return False, None, _msg

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, name, shop_name, password, status, review_note "
                "FROM `merchant` WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return False, None, "用户名不存在"
            if row["password"] != _hash_password(password):
                return False, None, "密码错误"
            # 平台客服确认之前，账号即使密码正确也不能登录
            status = row.get("status") or "approved"
            if status == "pending":
                return False, None, "注册申请尚待平台客服审核，审核通过后才能登录"
            if status == "rejected":
                note = row.get("review_note") or "请联系平台客服"
                return False, None, f"注册申请未通过审核：{note}"
            if status != "approved":
                return False, None, "账号状态异常，请联系平台客服"
            from 公共.platform_state import merchant_state
            if merchant_state(str(row["id"]))["disabled"]:
                return False, None, "该商户账号已被平台停用，如有疑问请联系平台"
            token = secrets.token_hex(16)
            cur.execute("UPDATE `merchant` SET token=%s WHERE id=%s", (token, row["id"]))
        conn.commit()
        info = _row_to_info(row)
        info["token"] = token
        return True, info, "登录成功"
    except Exception as e:
        conn.rollback()
        return False, None, f"登录失败：{e}"
    finally:
        conn.close()


def get_merchant_by_token(token):
    """按 token 反查商户，用于接口鉴权。找不到返回 None。"""
    token = str(token or "").strip()
    if not token:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, name, shop_name, token, status, "
                "reviewed_by, reviewed_at, review_note FROM `merchant` WHERE token=%s",
                (token,),
            )
            row = cur.fetchone()
        info = _row_to_info(row)
        if not info:
            return None
        # 被平台停用，或审核被驳回 / 尚未通过：令牌一律失效
        if info.get("disabled") or info.get("status") != "approved":
            return None
        return info
    except Exception:
        return None
    finally:
        conn.close()


def get_merchant(mid):
    try:
        mid = int(str(mid).strip())
    except (TypeError, ValueError):
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, name, shop_name FROM `merchant` WHERE id=%s", (mid,))
            row = cur.fetchone()
        return _row_to_info(row)
    except Exception:
        return None
    finally:
        conn.close()


def logout_merchant(token):
    token = str(token or "").strip()
    if not token:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE `merchant` SET token=NULL WHERE token=%s", (token,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------- 平台审核

def list_merchant_applications(status=None, limit=200):
    """平台客服查看商户注册申请。status 可为 pending / approved / rejected。"""
    sql = ("SELECT id, username, name, shop_name, status, create_time, "
           "reviewed_by, reviewed_at, review_note FROM `merchant`")
    args = []
    if status:
        sql += " WHERE `status`=%s"
        args.append(str(status))
    sql += " ORDER BY `create_time` DESC, `id` DESC LIMIT %s"
    args.append(min(int(limit or 200), 1000))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        for r in rows:
            for key in ("create_time", "reviewed_at"):
                if r.get(key):
                    r[key] = str(r[key])
        return rows
    finally:
        conn.close()


def review_merchant(mid, action, reviewer="", note=""):
    """平台客服确认 / 驳回商户注册。返回 (是否成功, 商户信息, 提示)。

    驳回会清空该商户当前登录令牌，已登录会话立即失效。
    """
    action = str(action or "").strip().lower()
    if action not in ("approve", "reject"):
        return False, None, "审核动作只能是 approve 或 reject"
    try:
        mid = int(str(mid).strip())
    except (TypeError, ValueError):
        return False, None, "商户编号无效"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, status FROM `merchant` WHERE id=%s", (mid,))
            row = cur.fetchone()
            if not row:
                return False, None, "商户不存在"
            new_status = "approved" if action == "approve" else "rejected"
            cur.execute(
                "UPDATE `merchant` SET `status`=%s, `reviewed_by`=%s, "
                "`reviewed_at`=NOW(), `review_note`=%s"
                + (", `token`=NULL" if action == "reject" else "") +
                " WHERE `id`=%s",
                (new_status, str(reviewer or "平台客服")[:64], str(note or "")[:255], mid),
            )
        conn.commit()
        msg = "审核通过，该商户现在可以登录" if action == "approve" else "已驳回申请，该商户无法登录"
        return True, {"id": str(mid), "username": row["username"], "status": new_status}, msg
    except Exception as e:
        conn.rollback()
        return False, None, f"审核失败：{e}"
    finally:
        conn.close()
