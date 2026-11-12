"""创建三个端的演示账号（顾客 / 商户 / 平台），统一为 admin / 12345。

清库后 cs.user、cs.merchant 会被清空，三端就没法登录了。这个脚本把演示账号补回来，
可以反复运行：已存在就跳过，不覆盖你改过的密码。

用法：
    python 工具/创建演示账号.py          创建缺失的账号
    python 工具/创建演示账号.py --reset  重置密码为 12345（账号已存在也改）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 公共.accounts import _hash_password, ensure_merchant_table  # noqa: E402
from 公共.db_config import get_conn  # noqa: E402

DEMO_NAME = "admin"
DEMO_PASSWORD = "12345"
DEMO_EMAIL = "demo@example.com"
DEMO_SHOP = "演示小店"
PLATFORM_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "售后", "admin.json")


def ensure_customer(reset):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM `user` WHERE name=%s", (DEMO_NAME,))
            row = cur.fetchone()
            if row and not reset:
                return "已存在，跳过"
            if row:
                cur.execute("UPDATE `user` SET password=%s WHERE id=%s", (_hash_password(DEMO_PASSWORD), row["id"]))
                conn.commit()
                return "已存在，密码已重置"
            cur.execute(
                "INSERT INTO `user` (name, age, sex, email, password) VALUES (%s, %s, %s, %s, %s)",
                (DEMO_NAME, 30, "未知", DEMO_EMAIL, _hash_password(DEMO_PASSWORD)),
            )
        conn.commit()
        return "已创建"
    finally:
        conn.close()


def ensure_merchant(reset):
    ok, msg = ensure_merchant_table()
    if not ok:
        return f"失败：{msg}"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM `merchant` WHERE username=%s", (DEMO_NAME,))
            row = cur.fetchone()
            if row and not reset:
                return "已存在，跳过"
            if row:
                cur.execute("UPDATE `merchant` SET password=%s WHERE id=%s", (_hash_password(DEMO_PASSWORD), row["id"]))
                conn.commit()
                return "已存在，密码已重置"
            cur.execute(
                "INSERT INTO `merchant` (username, password, name, shop_name) VALUES (%s, %s, %s, %s)",
                (DEMO_NAME, _hash_password(DEMO_PASSWORD), DEMO_NAME, DEMO_SHOP),
            )
        conn.commit()
        return "已创建"
    finally:
        conn.close()


def ensure_platform(reset):
    data = {"admin_list": []}
    if os.path.exists(PLATFORM_JSON):
        with open(PLATFORM_JSON, encoding="utf-8") as f:
            data = json.load(f) or data
    admins = data.setdefault("admin_list", [])
    entry = next((a for a in admins if DEMO_NAME in (a.get("user_id"), a.get("user_name"))), None)
    if entry:
        if not reset:
            return "已存在，跳过"
        entry["password"] = _hash_password(DEMO_PASSWORD)
        state = "已存在，密码已重置"
    else:
        admins.append({
            "user_id": DEMO_NAME,
            "user_name": DEMO_NAME,
            "password": _hash_password(DEMO_PASSWORD),
            "role": "平台管理员",
        })
        state = "已创建"
    with open(PLATFORM_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return state


def main():
    reset = "--reset" in sys.argv
    print(f"演示账号：{DEMO_NAME} / {DEMO_PASSWORD}" + ("（强制重置密码）" if reset else ""))
    for label, fn in (("顾客端", ensure_customer), ("商户端", ensure_merchant), ("平台端", ensure_platform)):
        try:
            print(f"  {label}：{fn(reset)}")
        except Exception as e:
            print(f"  {label}：失败 {e}")
            return 1
    print("完成。登录入口：/ 顾客、/merchant 商户、/platform 平台")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
