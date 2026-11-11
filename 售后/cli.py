"""
售后模块 —— 命令行对话流程。

入口统一在项目根目录 main.py：
    python main.py --cli

流程：
    run_cli()          入口：确保对话表存在 → 登录 → 打印权限 → 进入对话循环
    login()            登录：管理员查 admin.json，普通用户查 cs.user（拿到真实信息）
    print_user_scope() 打印当前用户的权限范围
    run_chat_loop()    对话循环：调 Dify → 权限校验 → 落库 → 分支打印 → 保存对话
"""
import json

from .workflow import run_dify_workflow
from .config import load_admin_config
from .auth import check_is_admin, check_ops_permission
from .user import verify_user
from .order import execute_ops, parse_ops_payload, build_user_orders
from .chat import ensure_chat_table, new_session_id, save_message

def login():
    """
    登录并返回用户信息 dict：
        {"name", "id", "role", "is_admin", "age", "sex"}
    登录失败返回 None。
    """
    admin_list = load_admin_config()
    try:
        user_name = input("用户姓名：").strip()
        user_id = input("用户ID：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n👋 未输入用户信息，程序退出")
        return None

    # 1) 先判管理员（admin.json）
    if check_is_admin(user_id, user_name, admin_list):
        print(f"✅ 识别为管理员账号：{user_name}({user_id})")
        return {
            "name": user_name, "id": user_id, "role": "admin",
            "is_admin": True, "age": None, "sex": None,
        }

    # 2) 普通用户：必须能在 cs.user 表里查到这个 ID，且姓名一致
    ok, info, msg = verify_user(user_id, user_name)
    if not ok:
        print(f"❌ 登录失败：{msg}")
        return None

    print(f"ℹ️ 普通用户账号：{info['name']}({info['id']})"
          f"  年龄={info.get('age')}  性别={info.get('sex')}")
    return {
        "name": info["name"], "id": str(info["id"]), "role": "user",
        "is_admin": False, "age": info.get("age"), "sex": info.get("sex"),
    }

def print_user_scope(user):
    """打印当前用户能操作的数据范围"""
    if user["is_admin"]:
        print("🔑 权限：管理员，可增删改查所有订单")
    else:
        print(f"🔒 权限：普通用户，仅能增删改查自己的订单（user_id={user['id']}）")

def print_ops(ops_list, biz_source):
    print(f"\n=====【业务操作JSON - 完整数组（来源字段：{biz_source}）】=====")
    print(json.dumps(ops_list, ensure_ascii=False, indent=2))
    print("\n=====【业务操作JSON - 逐条操作】=====")
    for idx, op in enumerate(ops_list):
        print(f"\n【操作{idx + 1}】operate = {op.get('operate')}")
        print(json.dumps(op, ensure_ascii=False, indent=2))

def run_ops(ops_list, user):
    """执行操作、打印结果，并返回给对话记录用的文本行"""
    print("\n=====【MySQL cs.order 表 - 执行结果】=====")
    db_results = execute_ops(
        ops_list,
        user_name=user["name"],
        user_id=user["id"],
        user_role=user["role"],
    )
    lines = []
    for res in db_results:
        flag = "✅" if res["success"] else "❌"
        print(f"\n{flag} 操作{res['index']} [{res['operate']}]：{res['message']}")
        if res.get("data"):
            print("   返回结果：" + json.dumps(res["data"], ensure_ascii=False))
        lines.append(f"[{res['operate']}] {res['message']}")
    return lines

def handle_business(biz_raw, biz_source, user):
    """把 Dify 返回的业务字段落地：非 JSON 只打印；JSON 先校验权限再执行。
    返回给对话记录用的文本行列表。"""
    ops_list, err_text = parse_ops_payload(biz_raw)

    # 1) 不是 JSON → 原样打印，不碰数据库
    if err_text is not None:
        text = (err_text if isinstance(err_text, str)
                else json.dumps(err_text, ensure_ascii=False, indent=2))
        print(f"\n=====【Dify 返回（非 JSON 业务数据，来源字段：{biz_source}）】=====")
        print(text)
        print("（该内容不是增删改查 JSON，已跳过数据库操作）")
        return [f"（非 JSON 业务数据）{text}"]

    # 2) 没有业务数据
    if not ops_list:
        print(f"\n=====【业务操作JSON】=====\n（来源字段 {biz_source}，但没有可执行的操作）")
        return []

    # 3) 合法 JSON → 打印 → 权限校验 → 执行
    print_ops(ops_list, biz_source)
    lines = []

    allowed, denied = check_ops_permission(ops_list, user)
    if denied:
        print("\n=====【权限校验】=====")
        for msg in denied:
            print(f"⛔ {msg}")
            lines.append(f"⛔ {msg}")
    if not allowed:
        print("⛔ 没有可执行的操作（无权限或记录不存在）")
        lines.append("⛔ 没有可执行的操作（无权限或记录不存在）")
        return lines

    lines += run_ops(allowed, user)
    return lines

def call_workflow(user_input, user):
    """调用 Dify 工作流。上传：库里的真实身份 + 当前用户已有的订单。"""
    print("\n=====正在调用Dify工作流=====")
    try:
        result = run_dify_workflow(
            user_input=user_input,
            user_id=user["id"],
            user_role=user["role"],
            user_name=user["name"],
            user_orders=build_user_orders(user),
        )
    except Exception as e:
        print(f"❌ 调用Dify工作流异常：{str(e)}")
        return None

    if not result["ok"]:
        print("❌ 调用Dify工作流失败，跳过后续业务逻辑")
        return None
    return result

def print_branches(result):
    """打印闲聊 / 安慰 / 节点错误 / 全空，并返回给对话记录用的文本行"""
    lines = []
    if result["talk"] is not None:
        print("\n=====【闲聊分支】=====")
        print(result["talk"])
        lines.append(result["talk"])
    if result["comfort"] is not None:
        print("\n=====【安慰分支】=====")
        print(result["comfort"])
        lines.append(result["comfort"])
    if result["node_error_message"] or result["node_error_type"]:
        print("\n=====【Dify 节点执行出错】=====")
        print(f"error_type: {result['node_error_type']}")
        print(f"error_message: {result['node_error_message']}")
        lines.append(f"[节点错误] {result['node_error_message']}")
    if (result["biz_raw"] is None
            and result["talk"] is None
            and result["comfort"] is None
            and not result["node_error_message"]
            and not result["node_error_type"]):
        print("\n⚠️ 无业务操作、无闲聊、无安慰返回")
        lines.append("（无业务操作、无闲聊、无安慰返回）")
    return lines

def ask_user_input():
    """读取一条对话内容，返回 None 表示退出"""
    print("\n=====请输入参数(输入exit退出)=====")
    try:
        text = input("用户对话内容：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n👋 输入结束，程序退出")
        return None
    if text == "exit":
        print("👋 程序退出")
        return None
    return text

def run_chat_loop(user):
    ensure_chat_table()
    session_id = new_session_id(user["id"])
    print(f"\n🧾 本次会话ID：{session_id}")

    while True:
        user_input = ask_user_input()
        if user_input is None:
            break

        # 用户这一句先存下来
        save_message(session_id, user["id"], user["name"], "user", user_input)

        result = call_workflow(user_input, user)
        if result is None:
            save_message(session_id, user["id"], user["name"], "assistant",
                         "（调用 Dify 工作流失败）")
            continue

        reply_lines = []
        try:
            reply_lines += handle_business(result["biz_raw"], result["biz_source"], user)
        except Exception as e:
            print(f"❌ 执行业务逻辑异常：{str(e)}")
            reply_lines.append(f"（执行业务逻辑异常：{e}）")

        reply_lines += print_branches(result)

        # 客服这一句存下来（闲聊/安慰/业务结果/错误）
        save_message(session_id, user["id"], user["name"], "assistant",
                     "\n".join(reply_lines))

def run_cli():
    ok, msg = ensure_chat_table()
    if not ok:
        print(f"⚠️ 对话记录表初始化失败：{msg}")

    user = login()
    if user is None:
        return
    print_user_scope(user)
    run_chat_loop(user)
