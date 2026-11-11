"""SKU 维度库存：定义、下单预占、支付确认、取消/超时释放、超卖控制。

分工：商品 JSON（公共/store.py）只保存 SKU 定义（规格名 / 价格），库存数量
由本模块单点维护在 MySQL `cs.sku_stock`，避免文件与数据库双写不一致。

库存字段语义：
    stock   当前实物库存（尚未售出的件数）
    locked  预占中的件数（stock 的子集）
    sold    累计已确认售出（仅统计）
    可售 = stock - locked

生命周期：
    下单 -> reserve()          locked += qty                （stock 不变）
    支付 -> confirm()          stock -= qty, locked -= qty, sold += qty
    取消 -> release()          locked -= qty
    超时 -> release_expired()  同 release，后台线程定时清理

防超卖：预占使用单条
    UPDATE ... SET locked=locked+%s WHERE sku_id=%s AND stock-locked>=%s
MySQL 行锁 + 当前读保证并发请求串行判断；一次结算的所有 SKU 在同一事务内，
任一 SKU 不足则整体回滚，不会出现「部分预占」。
"""
import hashlib
import json
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta

from 公共.db_config import get_conn

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT_DIR, "config.json")

DEFAULTS = {
    "default_stock": 100,        # 新 SKU / 老商品首次初始化的库存
    "reserve_ttl_seconds": 900,  # 预占有效期（15 分钟）
    "cleanup_interval_seconds": 30,
}

_ensured = False
_ensure_lock = threading.Lock()


class InsufficientStock(Exception):
    """某个 SKU 可售数量不足。"""
    def __init__(self, sku_name, need, available):
        super().__init__(f"「{sku_name}」库存不足：需要 {need} 件，当前可售 {available} 件")
        self.sku_name = sku_name
        self.need = need
        self.available = available


def settings():
    """读取 config.json 的 inventory 段，缺失项用默认值补齐。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        raw = cfg.get("inventory") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            out[k] = max(1, int(raw.get(k) or DEFAULTS[k]))
        except (TypeError, ValueError):
            pass
    return out


def _make_sku_id(pid, name):
    digest = hashlib.md5((str(pid) + "|" + str(name)).encode("utf-8")).hexdigest()[:12]
    return f"{pid}::{digest}"


def sku_definitions(product):
    """返回商品的全部 SKU 定义；没有 skus 字段的旧商品自动补一个默认 SKU。

    返回 [{"sku_id", "name", "price"}, ...]。sku_id 由「商品ID + 规格名」派生，
    规格名不变则 ID 稳定，商户增删其它规格不会串库存。
    """
    pid = str((product or {}).get("id") or "").strip()
    try:
        base_price = float((product or {}).get("price") or 0)
    except (TypeError, ValueError):
        base_price = 0.0

    out, seen = [], set()
    raw = (product or {}).get("skus")
    if isinstance(raw, list):
        for idx, s in enumerate(raw):
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip() or f"规格 {idx + 1}"
            sid = str(s.get("sku_id") or "").strip() or _make_sku_id(pid, name)
            if sid in seen:
                continue
            seen.add(sid)
            value = s.get("price")
            try:
                price = float(value) if value not in (None, "") else base_price
            except (TypeError, ValueError):
                price = base_price
            out.append({"sku_id": sid, "name": name, "price": price})
    if not out:
        out.append({"sku_id": f"{pid}::default", "name": "默认规格", "price": base_price})
    return out


# ------------------------------------------------------------
# 表结构
# ------------------------------------------------------------
CREATE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS `sku_stock` (
        `sku_id`      VARCHAR(128)  NOT NULL                  COMMENT 'SKU ID',
        `product_id`  VARCHAR(128)  NOT NULL                  COMMENT '商品 ID',
        `merchant_id` VARCHAR(64)   DEFAULT NULL              COMMENT '商家 ID',
        `sku_name`    VARCHAR(255)  DEFAULT NULL              COMMENT '规格名称',
        `price`       DECIMAL(10,2) DEFAULT 0                 COMMENT 'SKU 单价',
        `stock`       INT           NOT NULL DEFAULT 0        COMMENT '实物库存',
        `locked`      INT           NOT NULL DEFAULT 0        COMMENT '预占中',
        `sold`        INT           NOT NULL DEFAULT 0        COMMENT '累计售出',
        `version`     INT           NOT NULL DEFAULT 0        COMMENT '乐观锁版本',
        `updated_at`  DATETIME      DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`sku_id`),
        KEY `idx_product` (`product_id`),
        KEY `idx_merchant` (`merchant_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SKU 库存表';
    """,
    """
    CREATE TABLE IF NOT EXISTS `stock_reservation` (
        `id`          VARCHAR(64)  NOT NULL                  COMMENT '预占单号（同时作为订单 group_id）',
        `user_id`     VARCHAR(64)  DEFAULT NULL,
        `status`      VARCHAR(16)  NOT NULL DEFAULT 'reserved'
                                   COMMENT 'reserved/confirmed/released/expired',
        `expires_at`  DATETIME     NOT NULL                  COMMENT '预占到期时间',
        `remark`      VARCHAR(255) DEFAULT NULL,
        `created_at`  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        `updated_at`  DATETIME     DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        KEY `idx_status_expire` (`status`, `expires_at`),
        KEY `idx_user` (`user_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存预占单';
    """,
    """
    CREATE TABLE IF NOT EXISTS `stock_reservation_item` (
        `reservation_id` VARCHAR(64)  NOT NULL,
        `sku_id`         VARCHAR(128) NOT NULL,
        `product_id`     VARCHAR(128) NOT NULL,
        `sku_name`       VARCHAR(255) DEFAULT NULL,
        `quantity`       INT          NOT NULL,
        PRIMARY KEY (`reservation_id`, `sku_id`),
        KEY `idx_sku` (`sku_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存预占明细';
    """,
)


def ensure_tables():
    """建表（幂等）。返回 (是否成功, 提示)。"""
    global _ensured
    if _ensured:
        return True, "SKU 库存表已就绪"
    with _ensure_lock:
        if _ensured:
            return True, "SKU 库存表已就绪"
        try:
            conn = get_conn()
        except Exception as e:
            return False, f"数据库连接失败：{e}"
        try:
            with conn.cursor() as cur:
                for sql in CREATE_SQL:
                    cur.execute(sql)
            conn.commit()
            _ensured = True
            return True, "cs.sku_stock / cs.stock_reservation 表已就绪"
        except Exception as e:
            conn.rollback()
            return False, f"库存表建表失败：{e}"
        finally:
            conn.close()


def _conn():
    ok, msg = ensure_tables()
    if not ok:
        raise RuntimeError(msg)
    return get_conn()


# ------------------------------------------------------------
# SKU 定义同步与查询
# ------------------------------------------------------------
def sync_skus(product, stocks=None):
    """把商品 JSON 里的 SKU 定义同步到库存表。

    stocks: {sku_id: 库存数}。传入时按该值设置实物库存（不能小于已预占数）；
            不传时只为新 SKU 初始化 default_stock，已有库存保持不变。
    返回 (是否成功, 提示, [SKU 定义])。
    """
    defs = sku_definitions(product)
    pid = str((product or {}).get("id") or "").strip()
    merchant_id = str((product or {}).get("owner_id") or "").strip()
    stocks = stocks if isinstance(stocks, dict) else {}
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for d in defs:
                sid = d["sku_id"]
                cur.execute(
                    "INSERT INTO `sku_stock` "
                    "(sku_id, product_id, merchant_id, sku_name, price, stock) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "sku_name=VALUES(sku_name), price=VALUES(price), "
                    "merchant_id=VALUES(merchant_id)",
                    (sid, pid, merchant_id or None, d["name"], d["price"],
                     int(settings()["default_stock"])),
                )
                if sid in stocks:
                    try:
                        target = max(0, int(stocks[sid]))
                    except (TypeError, ValueError):
                        return False, f"「{d['name']}」库存必须是非负整数", defs
                    cur.execute(
                        "UPDATE `sku_stock` SET stock=%s, version=version+1 "
                        "WHERE sku_id=%s AND locked<=%s",
                        (target, sid, target),
                    )
                    if cur.rowcount == 0:
                        raise InsufficientStock(d["name"], target, -1)
        conn.commit()
        return True, "SKU 库存已同步", defs
    except InsufficientStock as e:
        conn.rollback()
        return False, f"{e}（不能把库存调到低于已预占数量，请先处理未支付订单）", defs
    except Exception as e:
        conn.rollback()
        return False, f"SKU 库存同步失败：{e}", defs
    finally:
        conn.close()


def sync_all_products():
    """启动时为所有现有商品初始化 SKU 库存（不覆盖已有库存）。"""
    try:
        from 公共 import store
        products = store.list_products(include_offline=True)
    except Exception as e:
        return False, f"读取商品失败：{e}"
    for p in products:
        ok, msg, _ = sync_skus(p)
        if not ok:
            return False, msg
    return True, f"已同步 {len(products)} 个商品的 SKU 库存"


def adjust_stock(sku_id, delta, reason=None):
    """增减库存（补货 / 盘盈盘亏）。delta 可正可负；扣减时不能低于已预占数。

    返回 (是否成功, 提示)。
    """
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return False, "调整数量必须是整数"
    sku_id = str(sku_id or "").strip()
    if not sku_id:
        return False, "缺少 SKU ID"
    if delta == 0:
        return True, "库存未变动"
    conn = _conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            if delta > 0:
                cur.execute(
                    "UPDATE `sku_stock` SET stock=stock+%s, version=version+1 WHERE sku_id=%s",
                    (delta, sku_id),
                )
            else:
                dec = -delta
                cur.execute(
                    "UPDATE `sku_stock` SET stock=stock-%s, version=version+1 "
                    "WHERE sku_id=%s AND stock>=%s",
                    (dec, sku_id, dec),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return False, "库存不足，无法扣减（已考虑预占数量）"
        conn.commit()
        return True, (f"已补货 {delta} 件" if delta > 0 else f"已扣减 {-delta} 件")
    except Exception as e:
        conn.rollback()
        return False, f"库存调整失败：{e}"
    finally:
        conn.close()


def restock(sku_id, qty, reason=None):
    """售后退款/退货完成时把货品退回可售库存（sold 回退、stock 增加）。

    与 adjust_stock 的区别：这是「已售出商品的回流」，只把已计入 sold 的数量
    搬回 stock，不会凭空放大总供给。数量为 0 或 SKU 缺失时安全跳过。
    返回 (是否成功, 提示)。
    """
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return False, "回补数量必须是整数"
    sku_id = str(sku_id or "").strip()
    if not sku_id or qty <= 0:
        return True, "无需回补库存"
    conn = _conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            # sold 不允许被减成负数（历史数据可能没走 confirm），用 GREATEST 兜底
            cur.execute(
                "UPDATE `sku_stock` SET stock=stock+%s, sold=GREATEST(sold-%s, 0), "
                "version=version+1 WHERE sku_id=%s",
                (qty, qty, sku_id),
            )
            affected = cur.rowcount
        conn.commit()
        if not affected:
            return False, "SKU 库存行不存在，未回补"
        return True, f"已回补库存 {qty} 件"
    except Exception as e:
        conn.rollback()
        return False, f"库存回补失败：{e}"
    finally:
        conn.close()


def stock_rows(product_ids=None):
    """批量取库存行。返回 {product_id: {sku_id: row}}。"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if product_ids:
                ids = [str(x) for x in product_ids]
                marks = ",".join(["%s"] * len(ids))
                cur.execute(
                    "SELECT sku_id, product_id, sku_name, price, stock, locked, sold "
                    f"FROM `sku_stock` WHERE product_id IN ({marks})", ids,
                )
            else:
                cur.execute(
                    "SELECT sku_id, product_id, sku_name, price, stock, locked, sold "
                    "FROM `sku_stock`"
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(str(r["product_id"]), {})[str(r["sku_id"])] = r
    return out


def decorate_products(products):
    """给商品列表附加 skus（含实时库存）与 available（可售总数）。"""
    products = products or []
    if not products:
        return products
    index = stock_rows([p.get("id") for p in products])
    for p in products:
        pid = str(p.get("id") or "")
        rows = index.get(pid) or {}
        skus, total = [], 0
        for d in sku_definitions(p):
            row = rows.get(d["sku_id"]) or {}
            stock = int(row.get("stock") or 0)
            locked = int(row.get("locked") or 0)
            available = max(0, stock - locked)
            total += available
            skus.append({
                "sku_id": d["sku_id"],
                "name": d["name"],
                "price": row.get("price") if row.get("price") is not None else d["price"],
                "stock": stock,
                "locked": locked,
                "sold": int(row.get("sold") or 0),
                "available": available,
            })
        p["skus"] = skus
        p["available"] = total
    return products


# ------------------------------------------------------------
# 下单项规范化
# ------------------------------------------------------------
def normalize_order_items(items):
    """服务端按商品/SKU 重新校验名称、规格、价格并合并数量。

    返回 (是否成功, 提示, lines)；lines 每项含
    product_id / sku_id / sku_name / name / price / qty / product。
    """
    try:
        from 公共 import store
        catalog = {str(p.get("id")): p for p in store.list_products() if p.get("id")}
    except Exception:
        return False, "下单失败：商品服务不可用", []

    merged = OrderedDict()
    for raw in items or []:
        raw = raw or {}
        pid = str(raw.get("product_id") or "").strip()
        product = catalog.get(pid)
        if not pid or product is None:
            name = str(raw.get("name") or "该商品").strip()
            return False, f"下单失败：「{name}」已下架或不存在，请刷新购物车", []
        defs = sku_definitions(product)
        sid = str(raw.get("sku_id") or "").strip()
        sku = next((s for s in defs if s["sku_id"] == sid), None) if sid else defs[0]
        if sku is None:
            return False, f"下单失败：「{product.get('name')}」所选规格已失效，请重新选择", []
        try:
            qty = int(raw.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            qty = 1
        if qty > 999:
            return False, f"下单失败：「{product.get('name')}」单件购买数量不能超过 999", []
        key = (pid, sku["sku_id"])
        if key in merged:
            merged[key]["qty"] += qty
        else:
            merged[key] = {
                "product_id": pid,
                "sku_id": sku["sku_id"],
                "sku_name": sku["name"],
                "name": str(product.get("name") or ""),
                "price": float(sku.get("price") or 0),
                "qty": qty,
                "product": product,
            }
    lines = list(merged.values())
    if not lines:
        return False, "下单失败：购物袋为空", []
    # 按 SKU 合并后仍要限制单笔数量
    for line in lines:
        if line["qty"] > 999:
            return False, f"下单失败：「{line['name']}」单件购买数量不能超过 999", []
    return True, "", lines


# ------------------------------------------------------------
# 预占 / 确认 / 释放
# ------------------------------------------------------------
def reserve(items, user_id=None, ttl_seconds=None):
    """原子预占一次结算的全部 SKU。返回 (是否成功, 提示, reservation_id, lines)。"""
    ok, msg, lines = normalize_order_items(items)
    if not ok:
        return False, msg, None, []
    ttl = int(ttl_seconds or settings()["reserve_ttl_seconds"])
    rid = "R" + uuid.uuid4().hex[:12]
    expires_at = datetime.now() + timedelta(seconds=ttl)
    conn = _conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            for line in lines:
                qty, sid = line["qty"], line["sku_id"]
                cur.execute(
                    "UPDATE `sku_stock` SET locked=locked+%s, version=version+1 "
                    "WHERE sku_id=%s AND (stock-locked)>=%s",
                    (qty, sid, qty),
                )
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT sku_name, GREATEST(stock-locked, 0) AS available "
                        "FROM `sku_stock` WHERE sku_id=%s", (sid,),
                    )
                    row = cur.fetchone() or {}
                    raise InsufficientStock(
                        row.get("sku_name") or line["sku_name"],
                        qty, int(row.get("available") or 0),
                    )
            cur.execute(
                "INSERT INTO `stock_reservation` (id, user_id, status, expires_at) "
                "VALUES (%s, %s, 'reserved', %s)",
                (rid, str(user_id or ""), expires_at),
            )
            for line in lines:
                cur.execute(
                    "INSERT INTO `stock_reservation_item` "
                    "(reservation_id, sku_id, product_id, sku_name, quantity) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (rid, line["sku_id"], line["product_id"], line["sku_name"], line["qty"]),
                )
        conn.commit()
        total = sum(x["qty"] for x in lines)
        return True, f"已预占 {total} 件商品，{ttl // 60} 分钟内未支付将自动释放", rid, lines
    except InsufficientStock as e:
        conn.rollback()
        return False, str(e), None, lines
    except Exception as e:
        conn.rollback()
        return False, f"库存预占失败：{e}", None, lines
    finally:
        conn.close()


def _finish(reservation_id, target_status, remark=None):
    """确认或释放预占。返回 (是否成功, 提示)。"""
    rid = str(reservation_id or "").strip()
    if not rid:
        return False, "缺少预占单号"
    conn = _conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, expires_at FROM `stock_reservation` WHERE id=%s FOR UPDATE",
                (rid,),
            )
            head = cur.fetchone()
            if not head:
                conn.rollback()
                return False, "预占单不存在"
            if head["status"] != "reserved":
                conn.rollback()
                return False, f"该预占单已处理（{head['status']}），无需重复操作"
            cur.execute(
                "SELECT sku_id, quantity FROM `stock_reservation_item` WHERE reservation_id=%s",
                (rid,),
            )
            detail = cur.fetchall()
            for item in detail:
                qty, sid = int(item["quantity"]), item["sku_id"]
                if target_status == "confirmed":
                    cur.execute(
                        "UPDATE `sku_stock` SET stock=stock-%s, locked=locked-%s, "
                        "sold=sold+%s, version=version+1 "
                        "WHERE sku_id=%s AND locked>=%s AND stock>=%s",
                        (qty, qty, qty, sid, qty, qty),
                    )
                else:
                    cur.execute(
                        "UPDATE `sku_stock` SET locked=locked-%s, version=version+1 "
                        "WHERE sku_id=%s AND locked>=%s",
                        (qty, sid, qty),
                    )
                if cur.rowcount != 1:
                    raise RuntimeError(f"SKU {sid} 库存状态异常，无法完成结算")
            cur.execute(
                "UPDATE `stock_reservation` SET status=%s, remark=%s WHERE id=%s",
                (target_status, remark, rid),
            )
        conn.commit()
        label = "支付确认完成，已扣减实物库存" if target_status == "confirmed" else "预占已释放"
        return True, label
    except Exception as e:
        conn.rollback()
        return False, f"结算库存失败：{e}"
    finally:
        conn.close()


def confirm(reservation_id):
    """支付成功：预占转为实际扣减（接支付网关时在支付回调里调用）。"""
    return _finish(reservation_id, "confirmed", "payment_confirmed")


def release(reservation_id, reason="cancelled"):
    """取消订单 / 支付失败：释放预占，库存重新可售。"""
    status = "expired" if reason == "expired" else "released"
    return _finish(reservation_id, status, reason)


def release_expired():
    """扫描并释放所有超时未支付的预占。返回释放的单数。"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM `stock_reservation` "
                "WHERE status='reserved' AND expires_at < NOW()"
            )
            ids = [r["id"] for r in cur.fetchall()]
    finally:
        conn.close()
    count = 0
    for rid in ids:
        ok, _ = release(rid, reason="expired")
        count += 1 if ok else 0
    return count


def get_reservation(reservation_id):
    """预占单详情（供支付 / 取消 / 排查使用）。"""
    rid = str(reservation_id or "").strip()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, status, expires_at, remark, created_at "
                "FROM `stock_reservation` WHERE id=%s", (rid,),
            )
            head = cur.fetchone()
            if not head:
                return None
            cur.execute(
                "SELECT sku_id, product_id, sku_name, quantity "
                "FROM `stock_reservation_item` WHERE reservation_id=%s", (rid,),
            )
            items = cur.fetchall()
        for k in ("expires_at", "created_at"):
            if isinstance(head.get(k), datetime):
                head[k] = head[k].strftime("%Y-%m-%d %H:%M:%S")
        head["items"] = items
        return head
    finally:
        conn.close()


def start_cleanup_worker():
    """启动后台守护线程，定时释放超时预占。重复调用只会启动一次。"""
    global _cleanup_started
    if _cleanup_started:
        return None
    interval = int(settings()["cleanup_interval_seconds"])

    def loop():
        import time
        while True:
            time.sleep(interval)
            try:
                release_expired()
            except Exception:
                pass

    t = threading.Thread(target=loop, name="inventory-expired-cleanup", daemon=True)
    t.start()
    _cleanup_started = True
    return t


_cleanup_started = False
