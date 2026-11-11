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
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商户账号表';
"""


def _hash_password(password):
    # 演示用途：加盐 SHA-256；生产建议换成 bcrypt / argon2
    return hashlib.sha256(("kaomianbao$" + str(password)).encode("utf-8")).hexdigest()


def ensure_merchant_table():
    """确保 cs.merchant 表存在。返回 (是否成功, 提示)。"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_MERCHANT_SQL)
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
    from 公共.platform_state import merchant_state
    flags = merchant_state(mid)
    return {
        "id": mid,
        "username": row["username"],
        "name": row["name"],
        "shop_name": row.get("shop_name") or "",
        "disabled": flags["disabled"],
        "muted": flags["muted"],
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
                "INSERT INTO `merchant` (username, password, name, shop_name) VALUES (%s, %s, %s, %s)",
                (username, _hash_password(password), name, shop_name or name),
            )
            mid = cur.lastrowid
        conn.commit()
        return True, {"id": str(mid), "username": username, "name": name, "shop_name": shop_name or name}, "注册成功"
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
                "SELECT id, username, name, shop_name, password FROM `merchant` WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return False, None, "用户名不存在"
            if row["password"] != _hash_password(password):
                return False, None, "密码错误"
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
                "SELECT id, username, name, shop_name, token FROM `merchant` WHERE token=%s",
                (token,),
            )
            row = cur.fetchone()
        info = _row_to_info(row)
        # 被平台停用的商家，token 立即失效
        if info and info.get("disabled"):
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
