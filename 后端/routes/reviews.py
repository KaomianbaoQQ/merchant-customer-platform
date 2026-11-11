"""商品评价接口：提交评价（登录 + 订单已签收） + 查询评价列表（匿名）。"""
from flask import Blueprint, jsonify, request

from 售后.review import (
    submit_review, list_reviews, review_summary, can_review, reviewable_order,
)
from ..common import resolve_user

bp = Blueprint("reviews", __name__, url_prefix="/api")


@bp.post("/products/<pid>/reviews")
def api_submit_review(pid):
    """提交商品评价。同一用户对同一商品再次提交视为更新。

    评价是订单完成之后的事：该商品必须先有一笔属于本人、已签收的订单。
    """
    data = request.get_json(silent=True) or {}
    user = resolve_user(data.get("user"))
    if user is None:
        return jsonify({"ok": False, "error": "请先登录后再评价"}), 401

    ok, msg, entry = submit_review(
        pid, user,
        rating=data.get("rating"),
        content=data.get("content"),
    )
    return jsonify({"ok": ok, "message": msg, "review": entry}), (200 if ok else 400)


@bp.get("/products/<pid>/reviews")
def api_list_reviews(pid):
    """查询商品评价列表（匿名展示）。

    带登录身份时会一并返回 can_review，前端据此决定是否显示评价表单。
    """
    summary = review_summary(pid)
    reviews = list_reviews(pid)
    user = resolve_user({
        "id": request.args.get("user_id"),
        "name": request.args.get("user_name"),
    })
    # 可以评价时，一并告诉前端「是哪笔已签收订单让评价成立」，界面才能明示
    order = reviewable_order(user["id"], pid) if user else None
    return jsonify({
        "ok": True,
        "count": summary["count"],
        "avg": summary["avg"],
        "reviews": reviews,
        "logged_in": user is not None,
        "can_review": order is not None,
        "review_order": order,
    })
