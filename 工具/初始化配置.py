"""初始化 / 修改配置 —— 把所有密钥写进 config.json（该文件不进版本库）。

代码里不再出现任何 API Key、邮箱授权码、数据库口令，全部从这里写入。

用法：
    .venv/bin/python 工具/初始化配置.py           # 交互式填写（直接回车 = 保留原值）
    .venv/bin/python 工具/初始化配置.py --show    # 查看当前配置（密钥脱敏）
    .venv/bin/python 工具/初始化配置.py --check   # 只检查哪些必填项还没填

所有配置项都可以用环境变量覆盖（不改文件），详见 公共/app_config.py 的 ENV_MAP。
"""
import getpass
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

# 交互式填写的字段：(配置路径, 中文名, 是否密文输入, 说明)
FIELDS = [
    ("database.host", "MySQL 主机", False, "通常 127.0.0.1"),
    ("database.port", "MySQL 端口", False, "默认 3306"),
    ("database.user", "MySQL 用户", False, "通常 root"),
    ("database.password", "MySQL 密码", True, ""),
    ("database.name", "数据库名", False, "默认 cs"),

    ("mongo.uri", "MongoDB 连接串", False, "通常 mongodb://127.0.0.1:27017"),
    ("mongo.database", "MongoDB 库名", False, "默认 testmall_chat"),

    ("express_tracking.api_key", "快递查询 API Key", True, "uapis.cn 的 Key，可留空（留空则只能用 7 位内部运单号）"),
    ("express_tracking.base_url", "快递查询接口地址", False, "默认 https://uapis.cn/api/v1"),

    ("dify.base_url", "Dify 地址", False, "默认 http://localhost/v1"),
    ("dify.workflows.order_locate.api_key", "Dify「订单定位」Key", True, "app- 开头"),
    ("dify.workflows.after_sales.api_key", "Dify「售后客服」Key", True, "app- 开头"),
    ("dify.workflows.consult.api_key", "Dify「商品咨询」Key", True, "app- 开头"),
    ("dify_knowledge.api_key", "Dify 知识库 Key", True, "dataset- 开头，用于建知识库"),
    ("dify_knowledge.dataset_id", "Dify 知识库 ID", False, ""),

    ("smtp.user", "SMTP 发件邮箱", False, "如 xxx@163.com"),
    ("smtp.password", "SMTP 授权码", True, "注意是授权码，不是登录密码"),
    ("smtp.host", "SMTP 服务器", False, "留空则按邮箱域名自动推断"),
    ("smtp.port", "SMTP 端口", False, "465(SSL) / 587(STARTTLS)"),
    ("smtp.from_name", "发件人显示名", False, "如「测试商城」"),
]

DEFAULTS = {
    "database.host": "127.0.0.1",
    "database.port": 3306,
    "database.user": "root",
    "database.name": "cs",
    "database.charset": "utf8mb4",
    "mongo.uri": "mongodb://127.0.0.1:27017",
    "mongo.database": "testmall_chat",
    "express_tracking.base_url": "https://uapis.cn/api/v1",
    "express_tracking.timeout": 30,
    "dify.base_url": "http://localhost/v1",
    "dify.workflows.order_locate.name": "订单定位",
    "dify.workflows.after_sales.name": "售后客服",
    "dify.workflows.consult.name": "商品咨询",
    "dify_knowledge.base_url": "http://localhost/v1",
    "dify_knowledge.map_file": "商户界面/knowledge_map.json",
    "dify_knowledge.product_dir": "商品咨询",
    "dify_knowledge.indexing_technique": "high_quality",
    "smtp.ssl": True,
    "smtp.from_name": "测试商城",
    "default_delay": 300,
    "inventory": {"default_stock": 100, "reserve_ttl_seconds": 900, "cleanup_interval_seconds": 30},
}


def read_raw():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ 现有 config.json 解析失败，将重新生成：{e}")
        return {}


def get_path(data, path, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def set_path(data, path, value):
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def mask(value):
    """密钥脱敏显示：保留头 3 尾 2。"""
    s = str(value or "")
    if not s:
        return "(未填写)"
    if len(s) <= 8:
        return "***"
    return s[:3] + "*" * max(3, len(s) - 5) + s[-2:]


def coerce(path, raw, old):
    """把用户输入转成合适的类型。"""
    if path.endswith("port"):
        try:
            return int(raw)
        except ValueError:
            print("   ⚠️ 端口必须是数字，保留原值")
            return old
    if path == "smtp.ssl":
        return str(raw).lower() in ("1", "true", "yes", "y", "是")
    return raw


def cmd_show():
    data = read_raw()
    if not data:
        print("config.json 不存在或为空。运行 .venv/bin/python 工具/初始化配置.py 开始填写。")
        return
    print("=" * 62)
    print(f"当前配置：{CONFIG_PATH}")
    print("=" * 62)
    for path, label, secret, _ in FIELDS:
        val = get_path(data, path, "")
        print(f"  {label:<18} {mask(val) if secret else (val if val != '' else '(未填写)')}")
    print("=" * 62)
    print("提示：密钥在此只显示脱敏后的样子，真实值请直接用编辑器打开 config.json 查看。")


def cmd_check():
    missing = []
    data = read_raw()
    for path, label, _secret, _hint in FIELDS:
        val = get_path(data, path, "")
        if path in ("express_tracking.api_key", "express_tracking.base_url",
                    "smtp.host", "smtp.port", "dify_knowledge.dataset_id"):
            continue  # 非必填
        if not str(val or "").strip():
            missing.append(label)
    if missing:
        print("❌ 以下必填项还没配置：")
        for m in missing:
            print("   -", m)
        print("\n运行 .venv/bin/python 工具/初始化配置.py 填写。")
    else:
        print("✅ 必填项都已配置。")


def ask_secret(prompt):
    """密文输入。没有终端（管道/CI）时退化为普通输入，避免 getpass 卡死。"""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return input(prompt)


def cmd_interactive():
    data = read_raw()
    for path, value in DEFAULTS.items():
        if get_path(data, path, None) in (None, ""):
            set_path(data, path, value)

    print("=" * 62)
    print("配置向导  —— 直接回车 = 保留当前值")
    print(f"写入：{CONFIG_PATH}")
    print("=" * 62)

    current_group = None
    groups = {
        "database.": "【一】MySQL 数据库",
        "mongo.": "【二】MongoDB（聊天记录）",
        "express_tracking.": "【三】快递查询接口（可选）",
        "dify.": "【四】Dify 工作流",
        "dify_knowledge.": "【五】Dify 知识库",
        "smtp.": "【六】发件邮箱",
    }

    for path, label, secret, hint in FIELDS:
        group = next((g for g in groups if path.startswith(g)), None)
        if group != current_group:
            current_group = group
            print(f"\n{groups.get(group, '')}")
        old = get_path(data, path, "")
        shown = mask(old) if secret else old
        tip = f"  ({hint})" if hint else ""
        prompt = f"  {label}{tip}\n    [{shown}] > "
        try:
            raw = ask_secret(prompt) if secret else input(prompt)
        except (KeyboardInterrupt, EOFError):
            print("\n已取消，未做任何改动。")
            return
        raw = raw.strip()
        if raw == "":
            continue
        set_path(data, path, coerce(path, raw, old))

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 62)
    print(f"✅ 已保存到 {CONFIG_PATH}")
    print("   该文件已被 .gitignore 排除，不会提交到 Git 仓库。")
    print("=" * 62)
    cmd_check()
    print("\n下一步：.venv/bin/python main.py 启动服务")


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if arg in ("--show", "-s"):
        cmd_show()
    elif arg in ("--check", "-c"):
        cmd_check()
    elif arg in ("--help", "-h"):
        print(__doc__)
    else:
        cmd_interactive()


if __name__ == "__main__":
    main()
