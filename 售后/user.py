"""cs.user 表：登录校验 + 用户信息查询 + 修改密码。

登录后 main 会把这里查到的用户信息（name / id）作为工作流入参上传给 Dify，
保证工作流拿到的身份信息是数据库里的真实值，而不是用户随手敲的。

修改密码有两种验证方式（见 change_password）：
    1. 常规：提供「原密码」校验；
    2. 忘记原密码：用「性别 + 收货地址」等个人资料核对（有该项资料才校验）。
"""
import re

from 公共.db_config import get_conn
from 公共.similarity import address_similarity, ADDRESS_MATCH_THRESHOLD

# cs.user 的邮箱列：用于「邮箱验证码登录 / 找回密码」
_EMAIL_COLUMN_READY = False


def ensure_user_email_column():
    """确保 cs.user 有 email 列（及唯一索引）。进程内只检查一次。

    返回 (是否成功, 提示)。老库首次调用会自动 ALTER TABLE，无需手工迁移。
    """
    global _EMAIL_COLUMN_READY
    if _EMAIL_COLUMN_READY:
        return True, ""

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user' AND COLUMN_NAME='email'"
            )
            if (cur.fetchone() or {}).get("c", 0) == 0:
                cur.execute(
                    "ALTER TABLE `user` ADD COLUMN `email` VARCHAR(128) DEFAULT NULL "
                    "COMMENT '邮箱（用于验证码登录/找回密码）' AFTER `sex`"
                )
            # 唯一索引：一个邮箱只能绑定一个账号（允许多个 NULL）
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user' AND INDEX_NAME='uniq_email'"
            )
            if (cur.fetchone() or {}).get("c", 0) == 0:
                try:
                    cur.execute("ALTER TABLE `user` ADD UNIQUE KEY `uniq_email` (`email`)")
                except Exception:
                    conn.rollback()
                    # 索引建失败（如历史数据重复）不阻塞主流程，返回时说明
                    return False, "email 列已就绪，但唯一索引创建失败（可能存在重复邮箱）"
        conn.commit()
        _EMAIL_COLUMN_READY = True
        return True, ""
    except Exception as e:
        conn.rollback()
        return False, f"email 列检查/创建失败：{e}"
    finally:
        conn.close()


def get_user(user_id):
    """
    按 ID 查 cs.user，返回 {"id", "name", "age", "sex"}；不存在返回 None。
    user.id 是 int，这里做一次安全转换，非法 ID 直接返回 None。
    """
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, age, sex FROM `user` WHERE id=%s", (uid,))
            return cur.fetchone()
    finally:
        conn.close()


def verify_user(user_id, user_name, password=None):
    """
    登录校验：ID 必须存在，姓名与库中一致，密码必填。
    返回 (是否通过, 用户信息 dict 或 None, 提示信息)
    """
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return False, None, f"用户ID「{user_id}」不合法"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, age, sex, password FROM `user` WHERE id=%s", (uid,))
            user = cur.fetchone()
    finally:
        conn.close()

    if user is None:
        return False, None, f"用户ID「{user_id}」在 cs.user 表中不存在"

    if user["name"] != (user_name or "").strip():
        return False, user, (
            f"姓名与ID不匹配：ID {user['id']} 对应的姓名是「{user['name']}」"
        )

    if password:
        from 公共.accounts import _hash_password
        if user.get("password") != _hash_password(password):
            return False, user, "密码错误"
    return True, user, "校验通过"


def verify_user_by_name(user_name, password):
    """
    仅按姓名 + 密码登录（用户登录界面不再输入 ID，ID 由平台维护）。
    若存在多个同姓名用户，只要密码匹配其中任意一个即视为登录成功。
    返回 (是否通过, 用户信息 dict 或 None, 提示信息)
    """
    name = str(user_name or "").strip()
    if not name:
        return False, None, "请输入姓名"
    if not password:
        return False, None, "请输入密码"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, age, sex, password FROM `user` WHERE name=%s ORDER BY id",
                (name,),
            )
            users = cur.fetchall()
    finally:
        conn.close()

    if not users:
        return False, None, f"姓名「{name}」不存在，请先注册"

    from 公共.accounts import _hash_password
    hashed = _hash_password(password)
    for u in users:
        if u.get("password") == hashed:
            return True, u, "校验通过"
    return False, users[0], "密码错误"


def _norm_addr(text):
    """地址比对前先去掉空格，避免「江西省 南昌市」与「江西省南昌市」判为不同。"""
    return re.sub(r"\s+", "", str(text or ""))


def verify_profile(user_id, sex=None, address=None):
    """按个人资料核对身份（忘记原密码时用于重置密码）。

    **有哪项资料就校验哪项，没有的跳过**：
        · 性别：账号里的性别不是「未知」时必须一致；
        · 地址：账号地址簿里有地址时，填写的地址必须命中其中一条。
    一项都核对不了（既没性别也没地址）就拒绝，避免无门槛改密。
    返回 (是否通过, 提示信息)。
    """
    user = get_user(user_id)
    if not user:
        return False, "账号不存在"

    checks = 0
    if sex:
        real_sex = str(user.get("sex") or "").strip()
        if real_sex and real_sex != "未知":
            checks += 1
            if str(sex).strip() != real_sex:
                return False, "性别与账号资料不符"
    if address:
        try:
            from 售后.address import list_addresses
            addrs = list_addresses(str(user_id)) or []
        except Exception:
            addrs = []
        if addrs:
            checks += 1
            # 地址无法要求一字不差，按相似度匹配：取与所有历史地址中的最高相似度
            sim_best = 0.0
            for a in addrs:
                sim = address_similarity(address, a.get("address"))
                if sim > sim_best:
                    sim_best = sim
            if sim_best < ADDRESS_MATCH_THRESHOLD:
                pct = int(round(sim_best * 100))
                thr = int(ADDRESS_MATCH_THRESHOLD * 100)
                return False, (
                    f"收货地址与账号资料不匹配（相似度 {pct}%，未达 {thr}% 阈值）。"
                    f"请尽量填写完整的曾用地址，例如「省 + 市 + 区 + 街道门牌」。"
                )

    if not checks:
        return False, "该账号没有可核对的性别 / 地址资料，请用原密码修改"
    return True, "资料核对通过"


def _resolve_id_by_name(name):
    """按姓名取第一个匹配的账号 ID；不存在返回 None。

    姓名可能重复，这里取 ID 最小的一条（与登录时「同名 + 正确密码即算登录」保持一致）。
    """
    name = str(name or "").strip()
    if not name:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM `user` WHERE name=%s ORDER BY id LIMIT 1", (name,))
            row = cur.fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def change_password(user_id=None, user_name=None, new_password=None, password2=None,
                    old_password=None, sex=None, address=None):
    """修改 / 重置顾客密码。

    · 传了 old_password：按原密码校验（常规方式）；
    · 没传 old_password：按「性别 + 地址」等资料核对（忘记原密码时使用）。
    user_id 缺省时可仅传 user_name，由姓名反查账号。
    返回 (是否成功, 提示信息)。
    """
    from 公共.accounts import _hash_password, validate_password

    ok, msg = validate_password(new_password, password2)
    if not ok:
        return False, msg

    uid = None
    if user_id:
        try:
            uid = int(str(user_id).strip())
        except (TypeError, ValueError):
            uid = None
    if uid is None and user_name:
        uid = _resolve_id_by_name(user_name)
    if uid is None:
        return False, "无法识别账号，请填写姓名"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, password FROM `user` WHERE id=%s", (uid,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False, "账号不存在"
    if user_name and str(row.get("name") or "") != str(user_name).strip():
        return False, "姓名与账号不匹配"

    if old_password:
        if row.get("password") != _hash_password(old_password):
            return False, "原密码不正确"
    else:
        ok, msg = verify_profile(uid, sex=sex, address=address)
        if not ok:
            return False, msg

    if row.get("password") == _hash_password(new_password):
        return False, "新密码不能与原密码相同"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE `user` SET password=%s WHERE id=%s",
                        (_hash_password(new_password), uid))
        conn.commit()
        return True, "密码已修改"
    except Exception as e:
        conn.rollback()
        return False, f"修改失败：{e}"
    finally:
        conn.close()


def reset_password_by_name(user_name, new_password, password2=None, sex=None, address=None):
    """忘记密码：未登录状态下，凭「姓名 + 性别 / 收货地址（相似匹配）」核对身份后重置密码。

    用于登录页「忘记密码」入口；与 change_password 的区别是调用方此刻没有登录态，
    只能提供姓名，由姓名反查账号再做资料核对。
    返回 (是否成功, 提示信息)。
    """
    from 公共.accounts import _hash_password, validate_password

    name = str(user_name or "").strip()
    if len(name) < 2:
        return False, "请填写账号姓名"
    ok, msg = validate_password(new_password, password2)
    if not ok:
        return False, msg

    uid = _resolve_id_by_name(name)
    if uid is None:
        return False, f"姓名「{name}」不存在，请确认后重试"

    ok, msg = verify_profile(uid, sex=sex, address=address)
    if not ok:
        return False, msg

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password FROM `user` WHERE id=%s", (uid,))
            row = cur.fetchone()
            if not row:
                return False, "账号不存在"
            if row.get("password") == _hash_password(new_password):
                return False, "新密码不能与原密码相同"
            cur.execute("UPDATE `user` SET password=%s WHERE id=%s",
                        (_hash_password(new_password), uid))
        conn.commit()
        return True, "密码已重置，请用新密码登录"
    except Exception as e:
        conn.rollback()
        return False, f"重置失败：{e}"
    finally:
        conn.close()


def register_user(name, password, age=None, sex="未知", password2=None, email=None):
    """
    客户注册：写入 cs.user（id 自增），返回 (是否成功, 用户信息 或 None, 提示)。

    邮箱为**必填**且必须唯一：邮箱是后续「验证码登录 / 忘记密码 / 找回账号」
    唯一的自助凭证，没有邮箱的账号在忘记密码时将无法自助找回。
    验证码在接口层（后端/routes/auth.py）已经校验过，这里只负责落库。
    """
    from 公共.accounts import _hash_password, validate_password

    name = str(name or "").strip()
    password = str(password or "")
    email = str(email or "").strip().lower()
    if len(name) < 2:
        return False, None, "请填写姓名（至少 2 个字）"
    ok, msg = validate_password(password, password2)
    if not ok:
        return False, None, msg
    if sex not in ("男", "女", "未知"):
        sex = "未知"
    try:
        age = int(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        age = None

    from 公共.email_code import is_valid_email
    if not email:
        return False, None, "请填写邮箱，注册必须绑定邮箱（用于验证码登录与找回账号）"
    if not is_valid_email(email):
        return False, None, "邮箱格式不正确"

    ensure_user_email_column()
    conn0 = get_conn()
    try:
        with conn0.cursor() as cur:
            cur.execute("SELECT id, name FROM `user` WHERE email=%s", (email,))
            taken = cur.fetchone()
            if taken:
                return False, None, f"该邮箱已绑定账号「{taken.get('name')}」，请直接登录或找回密码"
    finally:
        conn0.close()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `user` (name, age, sex, email, password) VALUES (%s, %s, %s, %s, %s)",
                (name, age, sex, email, _hash_password(password)),
            )
            uid = cur.lastrowid
        conn.commit()
        return True, {"id": str(uid), "name": name, "age": age, "sex": sex, "email": email}, "注册成功"
    except Exception as e:
        conn.rollback()
        return False, None, f"注册失败：{e}"
    finally:
        conn.close()


# ==================== 邮箱验证码登录 / 绑定 / 找回密码 ====================

def get_user_by_email(email):
    """按邮箱查账号，返回 {"id","name","age","sex","email"}；不存在返回 None。"""
    email = str(email or "").strip().lower()
    if not email:
        return None
    ensure_user_email_column()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, age, sex, email FROM `user` WHERE email=%s", (email,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "age": row.get("age"),
            "sex": row.get("sex"),
            "email": row.get("email"),
        }
    except Exception:
        return None
    finally:
        conn.close()


def email_bound_to_other(email, exclude_user_id=None):
    """邮箱是否已被【其它】账号占用。返回占用账号的姓名，未占用返回 None。"""
    email = str(email or "").strip().lower()
    if not email:
        return None
    ensure_user_email_column()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if exclude_user_id not in (None, ""):
                cur.execute(
                    "SELECT name FROM `user` WHERE email=%s AND id<>%s",
                    (email, int(exclude_user_id)),
                )
            else:
                cur.execute("SELECT name FROM `user` WHERE email=%s", (email,))
            row = cur.fetchone()
        return (row or {}).get("name")
    except Exception:
        return None
    finally:
        conn.close()


def verify_user_by_email_code(email, code):
    """邮箱验证码登录：邮箱必须已绑定账号，且验证码正确。

    返回 (是否通过, 用户信息 或 None, 提示信息)。
    """
    from 公共.email_code import verify_code, is_valid_email

    email = str(email or "").strip().lower()
    if not is_valid_email(email):
        return False, None, "邮箱格式不正确"

    user = get_user_by_email(email)
    if not user:
        return False, None, "该邮箱尚未绑定任何账号，请先用「邮箱验证码」注册，或登录后在个人中心绑定"

    ok, msg = verify_code(email, code, purpose="login")
    if not ok:
        return False, None, msg
    return True, user, "登录成功"


def reset_password_by_email_code(email, code, new_password, password2=None):
    """忘记密码：邮箱验证码核验通过后重置密码。

    返回 (是否成功, 提示信息)。
    """
    from 公共.accounts import _hash_password, validate_password
    from 公共.email_code import verify_code, is_valid_email

    email = str(email or "").strip().lower()
    if not is_valid_email(email):
        return False, "邮箱格式不正确"

    ok, msg = validate_password(new_password, password2)
    if not ok:
        return False, msg

    user = get_user_by_email(email)
    if not user:
        return False, "该邮箱尚未绑定任何账号"

    ok, msg = verify_code(email, code, purpose="reset")
    if not ok:
        return False, msg

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `user` SET password=%s WHERE id=%s",
                (_hash_password(new_password), int(user["id"])),
            )
        conn.commit()
        return True, "密码已重置，请用新密码登录"
    except Exception as e:
        conn.rollback()
        return False, f"重置失败：{e}"
    finally:
        conn.close()


def find_account_by_email_code(email, code):
    """忘记账号：用邮箱验证码核验身份后，返回该邮箱绑定的账号资料。

    验证码通过说明用户确实掌握该邮箱，此时给出账号名是安全的（否则用户
    无从登录）。返回 (是否成功, 用户信息 或 None, 提示)。
    """
    from 公共.email_code import verify_code, is_valid_email

    email = str(email or "").strip().lower()
    if not is_valid_email(email):
        return False, None, "邮箱格式不正确"
    if not str(code or "").strip():
        return False, None, "请输入邮箱验证码"

    user = get_user_by_email(email)
    if not user:
        return False, None, "该邮箱尚未绑定任何账号"

    ok, msg = verify_code(email, code, purpose="forgot")
    if not ok:
        return False, None, msg
    return True, user, "验证通过"


def bind_email(user_id, email, code):
    """已登录用户绑定/更换邮箱（需邮箱验证码）。

    返回 (是否成功, 提示信息, 是否换了新邮箱)。
    """
    from 公共.email_code import verify_code, is_valid_email

    email = str(email or "").strip().lower()
    if not is_valid_email(email):
        return False, "邮箱格式不正确", False
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return False, "账号ID不合法", False

    taken = email_bound_to_other(email, exclude_user_id=uid)
    if taken:
        return False, f"该邮箱已被账号「{taken}」绑定", False

    ok, msg = verify_code(email, code, purpose="bind")
    if not ok:
        return False, msg, False

    ensure_user_email_column()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE `user` SET email=%s WHERE id=%s", (email, uid))
            if cur.rowcount == 0:
                return False, "账号不存在", False
        conn.commit()
        return True, "邮箱绑定成功，之后可用邮箱验证码登录", True
    except Exception as e:
        conn.rollback()
        return False, f"绑定失败：{e}", False
    finally:
        conn.close()
