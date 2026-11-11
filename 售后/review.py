"""cs.review 表：商品评价。

匿名展示：查询时不返回 user_name，只返回脱敏后的匿名昵称
（保留姓氏 + 性别称呼，例如「张先生」「李女士」「用户***」）。

评价是**订单完成之后**的事：必须有一笔该商品、属于本人且已签收的订单才能评价，
未下单 / 未签收都不允许（见 can_review）。

同一用户对同一商品只能评价一次：再次提交视为更新原评价。
"""
import re

from 公共.db_config import get_conn

TABLE = "`review`"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `review` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT      COMMENT '评价自增ID',
    `product_id`  VARCHAR(128) NOT NULL                    COMMENT '商品ID',
    `user_id`     VARCHAR(64)  NOT NULL                     COMMENT '评价用户ID',
    `user_name`   VARCHAR(64)  NOT NULL                     COMMENT '评价用户真实姓名（不对外展示）',
    `user_sex`    VARCHAR(8)   DEFAULT '未知'                COMMENT '用户性别（用于匿名昵称）',
    `rating`      TINYINT      NOT NULL                     COMMENT '评分：1-5 星',
    `content`     TEXT                                       COMMENT '评价内容',
    `create_time` DATETIME     DEFAULT CURRENT_TIMESTAMP    COMMENT '评价时间',
    `update_time` DATETIME     DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP  COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_product_user` (`product_id`, `user_id`),
    KEY `idx_product_time` (`product_id`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品评价表';
"""


def ensure_tables():
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        return True, "review 表已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"建表失败：{e}"
    finally:
        conn.close()


def _anon_name(name, sex="未知"):
    """生成匿名昵称：保留姓氏 + 性别称呼，便于区分又不暴露全名。"""
    name = str(name or "").strip()
    if not name:
        return "匿名用户"
    sex = str(sex or "未知").strip()
    first = name[0]
    if sex == "男":
        return first + "先生"
    if sex == "女":
        return first + "女士"
    return first + "***"


def _entry(r):
    return {
        "id": r.get("id"),
        "product_id": r.get("product_id"),
        "rating": int(r.get("rating") or 0),
        "content": r.get("content") or "",
        "anon_name": _anon_name(r.get("user_name"), r.get("user_sex")),
        "create_time": (r.get("create_time") or "").strftime("%Y-%m-%d %H:%M")
        if hasattr(r.get("create_time"), "strftime") else str(r.get("create_time") or ""),
    }


def reviewable_order(user_id, product_id):
    """返回可用于评价的那笔订单（该商品、属于本人、**已签收**，取最近一笔）。

    评价是订单完成之后的事：没下过单、或还没签收，都没有可评价的订单，返回 None。
    返回 {"order_id", "goodsname", "quantity", "status_text", "waybill", "create_time"}。
    """
    uid = str(user_id or "").strip()
    pid = str(product_id or "").strip()
    if not uid or not pid:
        return None
    try:
        from .order import get_orders_by_user
        orders = get_orders_by_user(uid)          # 已按创建时间倒序
    except Exception:
        return None
    for o in orders:
        if str(o.get("product_id") or "") != pid:
            continue
        if int(o.get("status_step") or 0) >= 4:   # 4 = 已签收
            return {
                "order_id": o.get("id"),
                "goodsname": o.get("goodsname") or "",
                "quantity": int(o.get("quantity") or 0),
                "status_text": o.get("status_text") or "",
                "waybill": o.get("waybill") or "",
                "create_time": str(o.get("create_time") or ""),
            }
    return None


def can_review(user_id, product_id):
    """该用户能否评价这件商品：必须有一笔该商品、属于本人、**已签收**的订单。"""
    return reviewable_order(user_id, product_id) is not None


def submit_review(product_id, user, rating, content):
    """提交评价。同一用户对同一商品再次提交视为更新。

    只有该商品的订单已签收（订单完成）才允许评价。
    返回 (是否成功, 提示信息, 评价条目 dict 或 None)。
    """
    product_id = str(product_id or "").strip()
    if not product_id:
        return False, "缺少商品ID", None
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return False, "评分必须为 1-5 的整数", None
    if rating < 1 or rating > 5:
        return False, "评分必须为 1-5 的整数", None
    content = str(content or "").strip()
    if not content:
        return False, "请填写评价内容", None
    if len(content) > 500:
        return False, "评价内容不能超过 500 字", None

    user_id = str(user.get("id") or "").strip()
    user_name = str(user.get("name") or "").strip()
    user_sex = str(user.get("sex") or "未知").strip()
    if not user_id or not user_name:
        return False, "用户信息不完整", None

    # 评价是订单完成之后的事：该商品必须先有一笔已签收的订单
    if not can_review(user_id, product_id):
        return False, "订单完成后才能评价这件商品（确认收货后即可评价）", None

    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}", None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id FROM {TABLE} WHERE product_id=%s AND user_id=%s",
                (product_id, user_id),
            )
            existed = cur.fetchone()
            if existed:
                cur.execute(
                    f"UPDATE {TABLE} SET user_name=%s, user_sex=%s, rating=%s, content=%s "
                    "WHERE id=%s",
                    (user_name, user_sex, rating, content, existed["id"]),
                )
                review_id = existed["id"]
                msg = "评价已更新"
            else:
                cur.execute(
                    f"INSERT INTO {TABLE} "
                    "(product_id, user_id, user_name, user_sex, rating, content) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (product_id, user_id, user_name, user_sex, rating, content),
                )
                review_id = cur.lastrowid
                msg = "评价已提交"
            cur.execute(
                f"SELECT product_id, user_name, user_sex, rating, content, create_time "
                f"FROM {TABLE} WHERE id=%s",
                (review_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return True, msg, _entry(row)
    except Exception as e:
        conn.rollback()
        return False, f"提交评价失败：{e}", None
    finally:
        conn.close()


def list_reviews(product_id):
    """查询某商品的全部评价，匿名返回。"""
    product_id = str(product_id or "").strip()
    if not product_id:
        return []
    try:
        conn = get_conn()
    except Exception:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT product_id, user_name, user_sex, rating, content, create_time "
                f"FROM {TABLE} WHERE product_id=%s ORDER BY create_time DESC",
                (product_id,),
            )
            rows = cur.fetchall()
        return [_entry(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def review_summary(product_id):
    """返回商品评价统计：总数、平均分（用于前端展示）。"""
    product_id = str(product_id or "").strip()
    if not product_id:
        return {"count": 0, "avg": 0}
    try:
        conn = get_conn()
    except Exception:
        return {"count": 0, "avg": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt, COALESCE(AVG(rating), 0) AS avg "
                f"FROM {TABLE} WHERE product_id=%s",
                (product_id,),
            )
            r = cur.fetchone() or {}
        count = int(r.get("cnt") or 0)
        avg = round(float(r.get("avg") or 0), 1)
        return {"count": count, "avg": avg}
    except Exception:
        return {"count": 0, "avg": 0}
    finally:
        conn.close()
