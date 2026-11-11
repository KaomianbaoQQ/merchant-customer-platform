"""登录 / 注册接口（客户）。

管理员请使用独立的平台管理页（/platform）登录，其令牌是管理员身份的唯一凭据；
本接口只服务顾客，返回的账号一律 is_admin=False，不能凭这里拿到管理员权限。
"""
from flask import Blueprint, jsonify, request

from 公共.user_auth import issue_customer_token, revoke_customer_token
from 售后.user import (
    verify_user_by_name, register_user, change_password, reset_password_by_name,
    verify_user_by_email_code, reset_password_by_email_code, get_user_by_email,
    email_bound_to_other, bind_email, ensure_user_email_column,
    find_account_by_email_code,
)

bp = Blueprint("auth", __name__, url_prefix="/api")


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


@bp.post("/email/send-code")
def api_send_email_code():
    """发送邮箱验证码（用你自己的发件邮箱经 SMTP 发送）。

    入参：{"email": "...", "purpose": "login|register|reset|bind"}
    返回：{"ok": true, "message": "...", "expire_in": 300, "cooldown": 60}
    不会回传验证码本身（安全要求），验证码只出现在邮件里。
    """
    from 公共 import mailer
    from 公共.email_code import issue_code, is_valid_email, PURPOSES, CODE_TTL_SECONDS, RESEND_COOLDOWN_SECONDS

    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    purpose = str(data.get("purpose") or "login").strip().lower()

    if not is_valid_email(email):
        return jsonify({"ok": False, "error": "邮箱格式不正确"}), 400
    if purpose not in PURPOSES:
        return jsonify({"ok": False, "error": "验证码用途不合法"}), 400

    if not mailer.is_configured():
        hint = mailer.config_hint()
        return jsonify({
            "ok": False,
            "error": "发件邮箱尚未配置，请联系管理员在 config.json 的 smtp 段填写邮箱与授权码",
            "smtp": hint,
        }), 503

    ensure_user_email_column()

    # 按用途预检查账号状态，避免「等半天收码才发现邮箱没绑」
    if purpose in ("login", "reset", "forgot"):
        if not get_user_by_email(email):
            return jsonify({
                "ok": False,
                "error": "该邮箱尚未绑定任何账号，请先注册并绑定邮箱",
                "need_bind": True,
            }), 404
    elif purpose == "bind":
        owner = email_bound_to_other(email)
        if owner:
            return jsonify({"ok": False, "error": f"该邮箱已被账号「{owner}」绑定"}), 409
    elif purpose == "register":
        if get_user_by_email(email):
            return jsonify({"ok": False, "error": "该邮箱已注册，请直接登录或改用邮箱验证码登录"}), 409

    ok, code_or_msg, ttl = issue_code(email, purpose=purpose, ip=_client_ip())
    if not ok:
        return jsonify({"ok": False, "error": code_or_msg}), 429

    purpose_text = PURPOSES.get(purpose, "登录")
    sent, msg = mailer.send_mail(
        email,
        f"【Kaomianbao】{purpose_text}验证码",
        mailer.build_code_mail(code_or_msg, purpose_text, minutes=max(1, ttl // 60)),
        text_body=f"你的{purpose_text}验证码是 {code_or_msg}，{max(1, ttl // 60)} 分钟内有效。",
    )
    if not sent:
        return jsonify({"ok": False, "error": msg}), 502

    return jsonify({
        "ok": True,
        "message": f"验证码已发送至 {email}，请查收（含垃圾邮件箱）",
        "expire_in": ttl,
        "cooldown": RESEND_COOLDOWN_SECONDS,
        "smtp_from": mailer.config_hint().get("user"),
    })


@bp.post("/login")
def api_login():
    """顾客登录，两种方式：

    ① 姓名 + 密码：{"name": "...", "password": "..."}
    ② 邮箱验证码：{"email": "...", "code": "123456"}
    成功后统一签发顾客令牌（存 localStorage，后续请求带 Bearer）。
    """
    data = request.get_json(silent=True) or {}
    user_name = str(data.get("user_name") or data.get("name") or "").strip()
    password = str(data.get("password") or "")
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()

    # ② 邮箱验证码登录（带 code 且没给密码时优先走这条路）
    if email and code and not password:
        ok, info, msg = verify_user_by_email_code(email, code)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 401
        token = issue_customer_token(info)
        return jsonify({"ok": True, "token": token, "login_type": "email", "user": {
            "id": str(info["id"]), "name": info["name"], "role": "user", "is_admin": False,
            "age": info.get("age"), "sex": info.get("sex"), "email": info.get("email")}})

    if not user_name:
        return jsonify({"ok": False, "error": "请输入姓名或使用邮箱验证码登录"}), 401
    if not password:
        return jsonify({"ok": False, "error": "请输入密码"}), 401

    ok, info, msg = verify_user_by_name(user_name, password)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 401
    token = issue_customer_token(info)
    return jsonify({"ok": True, "token": token, "login_type": "password", "user": {
        "id": str(info["id"]), "name": info["name"], "role": "user",
        "is_admin": False, "age": info.get("age"), "sex": info.get("sex")}})


@bp.post("/logout")
def api_logout():
    """退出登录：吊销当前顾客令牌。"""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        revoke_customer_token(auth[7:])
    return jsonify({"ok": True, "message": "已退出登录"})


@bp.post("/change-password")
def api_change_password():
    """顾客修改 / 重置密码。

    两种方式二选一：
      · 常规：传 `old_password` + `new_password` + `password2`；
      · 忘记原密码：不传 `old_password`，改传 `sex` / `address` 等资料核对
        （账号里有什么资料就校验什么，没有的跳过）。
    """
    data = request.get_json(silent=True) or {}
    user = data.get("user") or {}
    ok, msg = change_password(
        user_id=user.get("id") or data.get("user_id"),
        user_name=user.get("name") or data.get("user_name"),
        new_password=data.get("new_password"),
        password2=data.get("password2"),
        old_password=data.get("old_password"),
        sex=data.get("sex"),
        address=data.get("address"),
    )
    return jsonify({"ok": ok, "message": msg, "error": None if ok else msg}), (200 if ok else 400)


@bp.post("/reset-password")
def api_reset_password():
    """忘记密码：**只接受邮箱验证码**这一种核对方式。

    入参：{"email": "...", "code": "123456", "new_password": "...", "password2": "..."}
    邮箱是账号唯一的自助凭证，因此不再提供「姓名 + 性别 + 地址相似度」这类
    可被撞库的弱校验通道（旧的资料核对接口已下线）。
    """
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()

    if not email:
        return jsonify({"ok": False, "error": "请填写账号绑定的邮箱"}), 400
    if not code:
        return jsonify({"ok": False, "error": "请填写邮箱验证码（先点「发送验证码」）"}), 400

    ok, msg = reset_password_by_email_code(
        email=email,
        code=code,
        new_password=data.get("new_password"),
        password2=data.get("password2"),
    )
    return jsonify({"ok": ok, "message": msg, "error": None if ok else msg}), (200 if ok else 400)


@bp.post("/forgot-account")
def api_forgot_account():
    """忘记账号名：用邮箱验证码核验身份后，返回该邮箱对应的账号资料。

    入参：{"email": "...", "code": "123456"}
    出参：{"ok": true, "account": {"name": "...", "user_id": "..."}}
    """
    data = request.get_json(silent=True) or {}
    ok, user, msg = find_account_by_email_code(
        email=data.get("email"),
        code=data.get("code"),
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({
        "ok": True,
        "message": "该邮箱对应的账号已找到",
        "account": {"name": user.get("name"), "user_id": str(user.get("id"))},
    })


@bp.post("/bind-email")
def api_bind_email():
    """已登录用户绑定邮箱：先点「发送验证码」（purpose=bind），再调本接口提交。"""
    from ..common import resolve_user
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录"}), 401

    ok, msg, changed = bind_email(
        user_id=user["id"],
        email=data.get("email"),
        code=data.get("code"),
    )
    if ok:
        # 邮箱变了，旧令牌里的信息仍是有效的（令牌按 id 绑定），这里回传最新资料
        info = get_user_by_email(data.get("email"))
        return jsonify({"ok": True, "message": msg, "email": (info or {}).get("email")})
    return jsonify({"ok": False, "error": msg}), 400


@bp.get("/email/status")
def api_email_status():
    """查询发件邮箱是否已配置（前端据此决定是否展示邮箱登录入口 / 提示配置）。

    公网访问（经隧道/代理）只返回「是否可用」，不暴露发件邮箱与 SMTP 主机，
    避免把发件地址与服务器信息泄露给外部访客。
    """
    from 公共 import mailer
    from ..public_access import is_external
    hint = mailer.config_hint()
    if is_external():
        return jsonify({"ok": True, "configured": hint["configured"]})
    return jsonify({
        "ok": True,
        "configured": hint["configured"],
        "from": hint["user"],
        "host": hint["host"],
        "port": hint["port"],
        "ssl": hint["ssl"],
    })


@bp.post("/register")
def api_register():
    """顾客注册。邮箱为**必填**项，且必须通过邮箱验证码校验。

    入参：{"name","password","password2","age","sex","email","code"}
    邮箱是登录以外的唯一自助凭证（验证码登录 / 找回账号 / 重置密码），
    因此注册阶段就必须验证邮箱真实可用，不允许跳过。
    """
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()

    from 公共.email_code import verify_code, is_valid_email
    if not email:
        return jsonify({"ok": False, "error": "请填写邮箱，注册必须绑定邮箱"}), 400
    if not is_valid_email(email):
        return jsonify({"ok": False, "error": "邮箱格式不正确"}), 400
    if not code:
        return jsonify({"ok": False, "error": "请填写邮箱验证码（先点「发送验证码」）"}), 400

    # 先做资料预校验：验证码一经校验即作废，若等校验完才发现姓名/密码不合法
    # 或邮箱已被占用，用户就得重新收码，体验很差。
    from 公共.accounts import validate_password
    if len(str(data.get("name") or "").strip()) < 2:
        return jsonify({"ok": False, "error": "请填写姓名（至少 2 个字）"}), 400
    ok_pwd, msg_pwd = validate_password(
        str(data.get("password") or ""), data.get("password2")
    )
    if not ok_pwd:
        return jsonify({"ok": False, "error": msg_pwd}), 400
    existing = get_user_by_email(email)
    if existing:
        return jsonify({
            "ok": False,
            "error": f"该邮箱已绑定账号「{existing.get('name')}」，请直接登录或找回密码",
        }), 409

    ok, msg = verify_code(email, code, purpose="register")
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    ok, info, msg = register_user(
        name=data.get("name"),
        password=data.get("password"),
        password2=data.get("password2"),
        age=data.get("age"),
        sex=data.get("sex") or "未知",
        email=email,
    )
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    token = issue_customer_token(info)
    return jsonify({"ok": True, "token": token, "user": {
        "id": info["id"], "name": info["name"], "role": "user",
        "is_admin": False, "age": info.get("age"), "sex": info.get("sex"),
        "email": info.get("email")},
        "message": msg}), 201