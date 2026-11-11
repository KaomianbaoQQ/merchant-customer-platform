"""售后模块 —— 权限校验。

    check_is_admin          管理员判定（读 admin.json 的管理员列表）
    check_ops_permission    对 Dify 返回的操作做前置校验
                            （普通用户只能操作属于自己 user_id 的订单）
"""
from .order import get_order_owner


def check_is_admin(user_id: str, user_name: str, admin_list: list) -> bool:
    """校验是否为管理员"""
    for admin in admin_list:
        if admin["user_id"] == user_id and admin["user_name"] == user_name:
            return True
    return False


def check_ops_permission(ops_list, user):
    """
    对 Dify 返回的操作做前置校验：
      - 管理员：全部放行
      - 普通用户：update / delete / 指定了ID的 select，目标订单必须属于自己
    返回 (可执行 ops, 被拒绝的提示列表)
    """
    if user["is_admin"]:
        return ops_list, []

    id_field = {"update": "update_id", "delete": "delete_id", "select": "query_id"}
    allowed, denied = [], []

    for op in ops_list:
        if not isinstance(op, dict):
            denied.append(f"非法操作（不是对象）：{op}")
            continue
        operate = str(op.get("operate", "")).strip().lower()
        field = id_field.get(operate)
        target_id = str(op.get(field) or "").strip() if field else ""

        # 只有"点名了具体订单ID"的操作才需要在这里判定归属；
        # 不带 ID 的 select / delete（按商品名）由 order.py 在 SQL 层限定 user_id
        if operate in id_field and target_id:
            owner = get_order_owner(target_id)
            if owner is None:
                denied.append(f"订单 {target_id} 不存在，已跳过")
                continue
            if str(owner) != str(user["id"]):
                denied.append(f"订单 {target_id} 属于用户 {owner}，你无权{operate}")
                continue

        allowed.append(op)
    return allowed, denied
