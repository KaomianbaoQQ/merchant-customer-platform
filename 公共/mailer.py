"""SMTP 发信模块：用「你自己的邮箱」作为发件人，给顾客发验证码 / 通知。

配置来源（优先级从高到低）：
    1. 环境变量  KB_SMTP_USER / KB_SMTP_PASSWORD / KB_SMTP_HOST / KB_SMTP_PORT / KB_SMTP_SSL / KB_SMTP_FROM_NAME
    2. config.json 的 "smtp" 段：
       {
         "smtp": {
           "user": "xxx@qq.com",          # 发件邮箱（登录 SMTP 的账号）
           "password": "授权码",           # 注意：QQ/163 等是「授权码」，不是登录密码
           "host": "",                    # 留空则按邮箱域名自动推断
           "port": 0,                     # 留空则按 host/ssl 自动推断
           "ssl": true,                   # 465 端口用 SSL；587 用 STARTTLS 时填 false
           "from_name": "考棉宝商城"
         }
       }

常用邮箱的 SMTP 参数已内置常见域名自动推断，正常情况下只需要填 user + password。
"""
import os
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

# 常见邮箱服务商：域名 -> (smtp 主机, 端口, 是否 SSL)
SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465, True),
    "foxmail.com": ("smtp.qq.com", 465, True),
    "vip.qq.com": ("smtp.qq.com", 465, True),
    "163.com": ("smtp.163.com", 465, True),
    "126.com": ("smtp.126.com", 465, True),
    "yeah.net": ("smtp.yeah.net", 465, True),
    "sina.com": ("smtp.sina.com", 465, True),
    "sina.cn": ("smtp.sina.cn", 465, True),
    "sohu.com": ("smtp.sohu.com", 465, True),
    "aliyun.com": ("smtp.aliyun.com", 465, True),
    "139.com": ("smtp.139.com", 465, True),
    "gmail.com": ("smtp.gmail.com", 587, False),
    "outlook.com": ("smtp.office365.com", 587, False),
    "hotmail.com": ("smtp.office365.com", 587, False),
    "live.com": ("smtp.office365.com", 587, False),
    "office365.com": ("smtp.office365.com", 587, False),
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def _load_smtp_conf():
    """读取 SMTP 配置（环境变量优先，其次 config.json），返回 dict。"""
    conf = {}
    try:
        import json
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        conf = (raw.get("smtp") or {}) if isinstance(raw, dict) else {}
    except Exception:
        conf = {}

    user = str(os.getenv("KB_SMTP_USER") or conf.get("user") or "").strip()
    password = str(os.getenv("KB_SMTP_PASSWORD") or conf.get("password") or "")
    host = str(os.getenv("KB_SMTP_HOST") or conf.get("host") or "").strip()
    port_raw = os.getenv("KB_SMTP_PORT") or conf.get("port") or 0
    ssl_raw = os.getenv("KB_SMTP_SSL")
    use_ssl = conf.get("ssl")
    from_name = str(os.getenv("KB_SMTP_FROM_NAME") or conf.get("from_name") or "考棉宝商城")

    try:
        port = int(port_raw or 0)
    except (TypeError, ValueError):
        port = 0

    if ssl_raw is not None and str(ssl_raw) != "":
        use_ssl = str(ssl_raw).strip().lower() in ("1", "true", "yes", "on")

    # 域名自动推断
    domain = user.split("@")[-1].lower() if "@" in user else ""
    preset = SMTP_PRESETS.get(domain)
    if not host and preset:
        host = preset[0]
    if not port and preset:
        port = preset[1]
    if use_ssl is None:
        use_ssl = preset[2] if preset else (port == 465)
    if not port:
        port = 465 if use_ssl else 587

    return {
        "user": user,
        "password": password,
        "host": host,
        "port": port,
        "ssl": bool(use_ssl),
        "from_name": from_name,
        "configured": bool(user and password and host),
    }


def is_configured():
    """SMTP 是否已配置完整（未配置时接口会明确提示，而不是静默失败）。"""
    return _load_smtp_conf()["configured"]


def config_hint():
    """给前端/日志看的配置提示（不含密码）。"""
    c = _load_smtp_conf()
    return {
        "configured": c["configured"],
        "user": c["user"],
        "host": c["host"],
        "port": c["port"],
        "ssl": c["ssl"],
    }


def _friendly_error(e):
    """把 SMTP 异常翻译成人能看懂的中文提示。"""
    text = f"{e}"
    low = text.lower()
    if "535" in text or "authentication failed" in low or "auth" in low and "fail" in low:
        return "SMTP 认证失败：请检查「授权码」是否正确（QQ/163 邮箱要用授权码，不是登录密码）"
    if "550" in text or "sender" in low and "rejected" in low:
        return "发件邮箱被拒绝：请确认发件地址与 SMTP 账号一致"
    if "timed out" in low or "timeout" in low:
        return "连接 SMTP 服务器超时：请检查网络或端口（465=SSL / 587=STARTTLS）"
    if "connection" in low or "network" in low or "getaddrinfo" in low:
        return "无法连接 SMTP 服务器：请检查 host / port 是否正确、网络是否可用"
    if "ssl" in low or "wrong version" in low:
        return "SSL 握手失败：465 端口应开启 SSL，587 端口应使用 STARTTLS（配置 ssl=false）"
    return f"发送失败：{text}"


def send_mail(to_email, subject, html_body, text_body=None, timeout=20):
    """发送一封邮件。返回 (是否成功, 提示信息)。

    to_email 支持单个地址或地址列表。
    """
    conf = _load_smtp_conf()
    if not conf["configured"]:
        return False, ("发件邮箱未配置：请在 config.json 的 smtp 段填写 user（发件邮箱）"
                       "与 password（授权码），或设置环境变量 KB_SMTP_USER / KB_SMTP_PASSWORD")

    to_email = str(to_email or "").strip()
    if "@" not in to_email:
        return False, "收件邮箱格式不正确"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(conf["from_name"], "utf-8")), conf["user"]))
    msg["To"] = to_email

    plain = text_body or "请使用支持 HTML 的邮件客户端查看本邮件。"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if conf["ssl"]:
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(conf["host"], conf["port"], timeout=timeout)
            server.ehlo()
            try:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            except smtplib.SMTPException:
                pass
        try:
            server.login(conf["user"], conf["password"])
            server.sendmail(conf["user"], [to_email], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, f"已发送到 {to_email}"
    except smtplib.SMTPAuthenticationError as e:
        return False, _friendly_error(e)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return False, _friendly_error(e)
    except Exception as e:
        return False, _friendly_error(e)


def build_code_mail(code, purpose_text="登录", minutes=5):
    """生成验证码邮件的 HTML 正文。"""
    return f"""\
<div style="margin:0;padding:24px;background:#f5f5f7;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;padding:32px 28px">
    <div style="font-size:20px;font-weight:700;color:#1d1d1f;margin-bottom:6px">考棉宝商城</div>
    <div style="font-size:13px;color:#86868b;margin-bottom:24px">邮箱验证码 · {purpose_text}</div>
    <div style="font-size:14px;color:#1d1d1f;line-height:1.7;margin-bottom:18px">
      你正在进行<strong>{purpose_text}</strong>操作，验证码如下：
    </div>
    <div style="background:#f5f5f7;border-radius:12px;padding:20px;text-align:center;margin-bottom:18px">
      <span style="font-size:34px;font-weight:700;letter-spacing:10px;color:#0071e3">{code}</span>
    </div>
    <div style="font-size:13px;color:#86868b;line-height:1.8">
      验证码 <strong>{minutes} 分钟</strong>内有效，请勿转发给他人。<br>
      如果这不是你本人的操作，忽略本邮件即可，你的账号不会有任何变化。
    </div>
    <div style="margin-top:24px;padding-top:18px;border-top:1px solid #eee;font-size:12px;color:#b0b0b5">
      本邮件由系统自动发送，请勿直接回复。
    </div>
  </div>
</div>"""
