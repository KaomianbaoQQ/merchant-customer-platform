"""配置发件邮箱（交互式），配置完成后可立即发一封测试邮件。

用法：
    cd /Users/kaomianbao/Downloads/模块
    .venv/bin/python 工具/配置发件邮箱.py

只需填「发件邮箱」和「授权码」两项，SMTP 服务器会按邮箱域名自动推断。
注意：QQ / 163 / 126 等邮箱要填的是「SMTP 授权码」，不是登录密码。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 公共 import mailer  # noqa: E402

CONFIG_PATH = mailer.CONFIG_PATH


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def main():
    cfg = load_config()
    cur = cfg.get("smtp") or {}

    print("=" * 58)
    print("配置发件邮箱（用于发送登录 / 找回密码验证码）")
    print("=" * 58)
    if cur.get("user"):
        print(f"当前配置：{cur.get('user')} @ {cur.get('host')}:{cur.get('port')} "
              f"({'SSL' if cur.get('ssl') else 'STARTTLS'})")

    user = input(f"\n发件邮箱 [{cur.get('user') or ''}]: ").strip() or cur.get("user", "")
    if not user:
        print("未填写发件邮箱，已退出。")
        return
    password = input("SMTP 授权码（不是登录密码）: ").strip() or cur.get("password", "")
    if not password:
        print("未填写授权码，已退出。")
        return
    from_name = input(f"发件人显示名 [{cur.get('from_name') or '测试商城'}]: ").strip() \
        or cur.get("from_name") or "测试商城"

    domain = user.split("@")[-1].lower()
    preset = mailer.SMTP_PRESETS.get(domain)
    if preset:
        host, port, use_ssl = preset
        print(f"\n已按域名自动识别：{host}:{port} ({'SSL' if use_ssl else 'STARTTLS'})")
    else:
        host = input("SMTP 服务器地址（如 smtp.example.com）: ").strip()
        port_raw = input("端口 [465]: ").strip()
        port = int(port_raw) if port_raw.isdigit() else 465
        use_ssl = (port == 465)
        print(f"使用 {host}:{port} ({'SSL' if use_ssl else 'STARTTLS'})")

    cfg["smtp"] = {
        "user": user,
        "password": password,
        "host": host,
        "port": port,
        "ssl": use_ssl,
        "from_name": from_name,
    }
    save_config(cfg)
    print(f"\n已写入 {CONFIG_PATH}")

    print("\n正在发送测试邮件到", user, "…")
    ok, msg = mailer.send_mail(
        user,
        "【测试商城】发件邮箱配置成功",
        mailer.build_code_mail("888888", "配置测试", minutes=5),
        text_body="发件邮箱配置成功，这是一封测试邮件。",
    )
    print(("✅ " if ok else "❌ ") + msg)
    if ok:
        print("\n配置完成，现在登录页「邮箱验证码」可以正常使用了。")
    else:
        print("\n请按上面的提示修正后重新运行本脚本。")


if __name__ == "__main__":
    main()
