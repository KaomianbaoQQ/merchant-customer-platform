"""配置文件：读取同目录 admin.json 中的管理员列表。"""
import json
import os


def load_admin_config():
    """读取同目录 admin.json 管理员配置"""
    config_path = os.path.join(os.path.dirname(__file__), "admin.json")
    if not os.path.exists(config_path):
        print(f"⚠️ 未找到配置文件 {config_path}，无管理员账号")
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("admin_list", [])


def verify_admin(name, password):
    """校验平台管理员账号（admin.json 的 admin_list，密码存加盐哈希）。

    账号名支持用 user_name 或 user_id 登录。
    返回 (是否通过, 管理员信息 dict 或 None, 提示信息)。
    """
    from 公共.accounts import _hash_password

    name = str(name or "").strip()
    password = str(password or "")
    if not name:
        return False, None, "请输入管理员账号"
    if not password:
        return False, None, "请输入密码"

    hashed = _hash_password(password)
    for admin in load_admin_config():
        uid = str(admin.get("user_id") or "").strip()
        uname = str(admin.get("user_name") or "").strip()
        if name not in (uid, uname):
            continue
        if str(admin.get("password") or "") != hashed:
            return False, None, "密码错误"
        return True, {"id": uid or uname, "name": uname or uid,
                      "role": admin.get("role") or "平台管理员", "is_admin": True}, "登录成功"
    return False, None, "管理员账号不存在"