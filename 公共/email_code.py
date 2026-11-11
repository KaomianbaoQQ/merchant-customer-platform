"""邮箱验证码：生成 / 落库 / 校验（MySQL cs.email_code 表）。

安全约束（演示/毕设场景够用，且能挡住最基本的滥用）：
    · 6 位数字，默认 5 分钟有效；
    · 同一邮箱 + 同一用途 60 秒冷却，避免连续轰炸；
    · 同一邮箱每小时最多发 10 条；
    · 单条验证码最多试错 5 次，超过即作废（防暴力枚举 6 位码）；
    · 校验成功立即标记 used，同一验证码不能复用。
"""
import random
import re
from datetime import datetime, timedelta

from 公共.db_config import get_conn

CODE_TTL_SECONDS = 300          # 验证码有效期 5 分钟
RESEND_COOLDOWN_SECONDS = 60    # 重发冷却 60 秒
HOURLY_LIMIT = 10               # 每邮箱每小时最多 10 条
MAX_ATTEMPTS = 5                # 单条验证码最多试错次数
CODE_LEN = 6
IP_HOURLY_LIMIT = 20            # 同一来源 IP 每小时最多 20 条（公网暴露后的兜底）

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

PURPOSES = {
    "login": "登录",
    "register": "注册",
    "reset": "重置密码",
    "forgot": "找回账号",
    "bind": "绑定邮箱",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `email_code` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT  COMMENT '主键',
    `email`       VARCHAR(128) NOT NULL                COMMENT '收件邮箱',
    `code`        VARCHAR(8)   NOT NULL                COMMENT '验证码',
    `purpose`     VARCHAR(16)  NOT NULL DEFAULT 'login' COMMENT '用途 login/register/reset/bind',
    `expire_at`   DATETIME     NOT NULL                COMMENT '过期时间',
    `used`        TINYINT      NOT NULL DEFAULT 0      COMMENT '是否已使用',
    `attempts`    INT          NOT NULL DEFAULT 0      COMMENT '已试错次数',
    `ip`          VARCHAR(64)  DEFAULT NULL            COMMENT '请求来源IP',
    `create_time` DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_email_purpose` (`email`, `purpose`, `used`),
    KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邮箱验证码表';
"""

_table_ready = False


def ensure_table():
    """确保 email_code 表存在（进程内只建一次）。返回 (是否成功, 提示)。"""
    global _table_ready
    if _table_ready:
        return True, ""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        _table_ready = True
        return True, ""
    except Exception as e:
        conn.rollback()
        return False, f"建表失败：{e}"
    finally:
        conn.close()


def normalize_email(email):
    return str(email or "").strip().lower()


def is_valid_email(email):
    return bool(EMAIL_RE.match(normalize_email(email)))


def _gen_code():
    return "".join(str(random.randint(0, 9)) for _ in range(CODE_LEN))


def check_cooldown(email, purpose):
    """检查是否处于重发冷却 / 小时限频中。

    返回 (是否可发送, 说明, 剩余秒数)。
    """
    email = normalize_email(email)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 冷却：最近一条是否在 60 秒内
            cur.execute(
                "SELECT create_time FROM `email_code` WHERE email=%s AND purpose=%s "
                "ORDER BY id DESC LIMIT 1",
                (email, purpose),
            )
            row = cur.fetchone()
            if row and row.get("create_time"):
                elapsed = (datetime.now() - row["create_time"]).total_seconds()
                if elapsed < RESEND_COOLDOWN_SECONDS:
                    wait = int(RESEND_COOLDOWN_SECONDS - elapsed) + 1
                    return False, f"发送过于频繁，请 {wait} 秒后再试", wait

            # 小时限频
            cur.execute(
                "SELECT COUNT(*) AS c FROM `email_code` "
                "WHERE email=%s AND create_time >= %s",
                (email, datetime.now() - timedelta(hours=1)),
            )
            cnt = (cur.fetchone() or {}).get("c", 0)
            if cnt >= HOURLY_LIMIT:
                return False, f"该邮箱 1 小时内发送次数已达上限（{HOURLY_LIMIT} 条），请稍后再试", 0
        return True, "", 0
    finally:
        conn.close()


def check_ip_limit(ip):
    """同一来源 IP 的小时级限流（防公网被当发信机刷）。

    返回 (是否可发送, 说明)。
    """
    ip = str(ip or "").strip()
    if not ip:
        return True, ""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM `email_code` WHERE ip=%s AND create_time >= %s",
                (ip[:64], datetime.now() - timedelta(hours=1)),
            )
            cnt = (cur.fetchone() or {}).get("c", 0)
        if cnt >= IP_HOURLY_LIMIT:
            return False, "当前网络发送验证码过于频繁，请稍后再试"
        return True, ""
    finally:
        conn.close()


def issue_code(email, purpose="login", ip=None):
    """生成并保存一条验证码（不负责发信）。

    返回 (是否成功, 验证码或错误提示, 有效期秒数)。
    """
    email = normalize_email(email)
    if not is_valid_email(email):
        return False, "邮箱格式不正确", 0
    if purpose not in PURPOSES:
        return False, "验证码用途不合法", 0

    ok, msg = ensure_table()
    if not ok:
        return False, msg, 0

    can, why, _sec = check_cooldown(email, purpose)
    if not can:
        return False, why, 0

    can_ip, why_ip = check_ip_limit(ip)
    if not can_ip:
        return False, why_ip, 0

    code = _gen_code()
    expire_at = datetime.now() + timedelta(seconds=CODE_TTL_SECONDS)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 同用途的历史未用码作废，避免多个有效码并存
            cur.execute(
                "UPDATE `email_code` SET used=1 WHERE email=%s AND purpose=%s AND used=0",
                (email, purpose),
            )
            cur.execute(
                "INSERT INTO `email_code` (email, code, purpose, expire_at, ip) "
                "VALUES (%s, %s, %s, %s, %s)",
                (email, code, purpose, expire_at, (ip or "")[:64] or None),
            )
        conn.commit()
        return True, code, CODE_TTL_SECONDS
    except Exception as e:
        conn.rollback()
        return False, f"验证码保存失败：{e}", 0
    finally:
        conn.close()


def verify_code(email, code, purpose="login", consume=True):
    """校验验证码。

    consume=True 时校验成功即标记已用（登录/注册/重置都用 True）。
    返回 (是否通过, 提示信息)。
    """
    email = normalize_email(email)
    code = str(code or "").strip()
    if not is_valid_email(email):
        return False, "邮箱格式不正确"
    if not code:
        return False, "请输入验证码"
    if purpose not in PURPOSES:
        return False, "验证码用途不合法"

    ok, msg = ensure_table()
    if not ok:
        return False, msg

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, code, attempts, expire_at, used FROM `email_code` "
                "WHERE email=%s AND purpose=%s ORDER BY id DESC LIMIT 1",
                (email, purpose),
            )
            row = cur.fetchone()
            if not row:
                return False, "请先获取验证码"
            if row.get("used"):
                return False, "该验证码已使用，请重新获取"
            if row.get("expire_at") and row["expire_at"] < datetime.now():
                return False, "验证码已过期，请重新获取"
            if int(row.get("attempts") or 0) >= MAX_ATTEMPTS:
                return False, "验证码试错次数过多已失效，请重新获取"

            if str(row["code"]) != code:
                cur.execute(
                    "UPDATE `email_code` SET attempts=attempts+1 WHERE id=%s", (row["id"],)
                )
                conn.commit()
                left = MAX_ATTEMPTS - (int(row.get("attempts") or 0) + 1)
                if left <= 0:
                    return False, "验证码错误次数过多已失效，请重新获取"
                return False, f"验证码不正确，还可再试 {left} 次"

            if consume:
                cur.execute("UPDATE `email_code` SET used=1 WHERE id=%s", (row["id"],))
                conn.commit()
            return True, "验证通过"
    except Exception as e:
        conn.rollback()
        return False, f"验证码校验失败：{e}"
    finally:
        conn.close()


def cleanup_expired(days=3):
    """清理 3 天前的验证码记录（可挂到定时任务，避免表无限增长）。"""
    ok, msg = ensure_table()
    if not ok:
        return 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM `email_code` WHERE create_time < %s",
                (datetime.now() - timedelta(days=days),),
            )
            n = cur.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()
