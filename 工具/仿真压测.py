#!/usr/bin/env python3
"""仿真压测：模拟上千个顾客 + 多个商家的完整业务链路。

链路（全部走**真实 HTTP 接口**，产生的订单 / 售后 / 聊天记录与真人操作完全一致）：
    商家注册 → 商家上架商品 → 顾客注册 → 存收货地址 → 客服咨询 → 下单 →
    申请退款 / 退货 → 商家处理 → 物流流转 → 结束

几个关键取舍：
  1. 客服会话**抽样真调 Dify**（默认 3%），其余直接写 MongoDB 模拟回复。
     真调 Dify 每条约 6.7 秒，几千条会跑几个小时，所以默认只抽样验证链路。
  2. 仿真期间把 config.json 的 default_delay 临时改成 1 秒（跑完**自动还原**），
     这样「发货 → 签收 → 退货 → 寄回 → 完成」能立刻流转，不用真等 5 分钟。
  3. 并发默认 24：MySQL max_connections 只有 151，并发开到几千会直接连不上。

产出（默认写到 结果/仿真_时间戳/）：
    users/<顾客ID>.json   每个顾客的操作流水 + 完整对话 + 最终订单/售后状态
    users.csv             一行一个顾客的汇总
    对话.jsonl            全部对话（一行一条）
    summary.json          总汇总（成功率 / 各步骤耗时 / 结果分布 / 数据库行数）
    失败清单.txt          失败步骤清单（没有则不生成）

用法：
    .venv/bin/python 工具/仿真压测.py                      # 2000 用户 / 并发 24
    .venv/bin/python 工具/仿真压测.py --users 50           # 小规模先跑通
    .venv/bin/python 工具/仿真压测.py --yes                # 跳过确认提示
    .venv/bin/python 工具/仿真压测.py --dify-rate 0.1      # 10% 走真 Dify
    .venv/bin/python 工具/仿真压测.py --no-short-delay     # 不改物流等待时间
"""
import argparse
import csv
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

DEFAULT_BASE = "http://127.0.0.1:8000"
PASSWORD = "pw123456"

TAGS = ["办公好物", "数码配件", "家居生活", "学习用品"]

# 顾客会问的问题（模拟会话用；真调 Dify 时也从这里抽）
QUESTIONS = [
    "这个支持蓝牙吗？", "保修多久？", "有现货吗？多久能到？", "能便宜点吗？",
    "这个和另一款比哪个好？", "重量是多少？", "支持货到付款吗？", "发什么快递？",
    "我是学生可以优惠吗？", "颜色有哪些？",
]
# 模拟的机器人回复（真调 Dify 时用真实回复，这里只是让对话记录看起来合理）
BOT_REPLIES = [
    "支持的，这款商品的参数里都有写，需要我帮你看哪一项？",
    "这款商品提供一年质保，非人为损坏都可以申请售后。",
    "有现货的，下单后商家会尽快发货，发货后可在「我的订单」查物流。",
    "价格已经是活动价了，多买几件可以联系商家客服咨询。",
    "两款各有侧重，你可以告诉我主要用途，我帮你对比一下。",
    "具体重量参数请以商品详情页为准。",
    "目前支持在线支付，下单后在「我的订单」可以看到进度。",
    "由商家统一发货，具体快递以运单号为准。",
    "学生优惠请直接联系商家客服确认。",
    "颜色款式以商品详情页展示为准。",
]

_thread_local = threading.local()
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def err(data):
    """接口失败时的提示文字：有的接口用 error，有的用 message，统一取一下。"""
    return data.get("error") or data.get("message") or ""


# ------------------------------------------------------------
# HTTP 工具
# ------------------------------------------------------------
class Api:
    """一个线程一个 Session（keep-alive），带简单重试。"""

    def __init__(self, base):
        self.base = base.rstrip("/")

    @property
    def session(self):
        s = getattr(_thread_local, "session", None)
        if s is None:
            s = requests.Session()
            _thread_local.session = s
        return s

    def post(self, path, body=None, token=None, timeout=180, retries=2):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.session.post(url, json=body or {}, headers=headers, timeout=timeout)
                try:
                    data = r.json()
                except ValueError:
                    data = {"ok": False, "error": f"响应非 JSON（HTTP {r.status_code}）"}
                return r.status_code, data
            except requests.RequestException as e:
                last = e
                if attempt < retries:
                    time.sleep(0.3 * (attempt + 1))
        return 0, {"ok": False, "error": f"请求失败：{last}"}

    def get(self, path, timeout=60, retries=2):
        url = self.base + path
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, timeout=timeout)
                try:
                    return r.status_code, r.json()
                except ValueError:
                    return r.status_code, {"ok": False, "error": "响应非 JSON"}
            except requests.RequestException as e:
                last = e
                if attempt < retries:
                    time.sleep(0.3 * (attempt + 1))
        return 0, {"ok": False, "error": f"请求失败：{last}"}


# ------------------------------------------------------------
# config.json 的 default_delay 临时调整（保留其它配置，跑完还原）
# ------------------------------------------------------------
def read_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def set_default_delay(seconds):
    cfg = read_config()
    cfg["default_delay"] = int(seconds)
    write_config(cfg)


# ------------------------------------------------------------
# 仿真主体
# ------------------------------------------------------------
class Simulation:
    def __init__(self, args):
        self.args = args
        self.api = Api(args.base_url)
        self.merchants = []          # [{"id","token","shop_name","products":[...]}]
        self.products = []           # 全部商品
        self.merchant_token = {}     # merchant_id -> token
        self.results = []
        self.region_pairs = []
        self.out_dir = None
        # 每次运行一个短标识：顾客姓名带上它，重复跑不会与上一次的账号重名
        self.tag = datetime.now().strftime("%H%M%S")

    # ---------- 准备 ----------
    def check_ready(self):
        code, data = self.api.get("/api/health", timeout=8)
        if code != 200 or not data.get("ok"):
            log(f"后端不可用（{self.args.base_url}/api/health）：{data.get('error') or code}")
            return False
        chat = str(data.get("chat") or "")
        if "失败" in chat:
            log(f"⚠️ MongoDB 不可用，聊天记录写不进去：{chat[:80]}")
            log("   先执行「一键启动全部.command」或 docker start kaomianbao-mongo 再跑")
        else:
            log(f"后端就绪：{chat}")
        return True

    def load_regions(self):
        """从行政区划表里取合法省 / 市 / 区组合（下单地址必须通过校验）。"""
        from 公共.regions import load_regions
        data = load_regions() or {}
        pairs = []
        for prov, cities in data.items():
            for city, districts in (cities or {}).items():
                for dist in (districts or []):
                    pairs.append((prov, city, dist))
        random.shuffle(pairs)
        self.region_pairs = pairs[:400] or [("江西省", "南昌市", "东湖区")]
        log(f"载入合法省市区组合 {len(self.region_pairs)} 组")

    def make_merchants(self):
        n = self.args.merchants
        for i in range(1, n + 1):
            username = f"shop{i}_{int(time.time()) % 100000}"
            shop = f"仿真店铺{i}"
            code, data = self.api.post("/api/merchant/register", {
                "username": username, "password": PASSWORD, "password2": PASSWORD,
                "name": f"店主{i}", "shop_name": shop,
            })
            if not data.get("ok"):
                log(f"商家注册失败：{data.get('error')}")
                continue
            mid = str(data["merchant"]["id"])
            code, data = self.api.post("/api/merchant/login",
                                       {"username": username, "password": PASSWORD})
            token = data.get("token") or ""
            self.merchant_token[mid] = token

            products = []
            for k in range(1, self.args.products + 1):
                name = f"{shop}·仿真商品{k}"
                c2, d2 = self.api.post("/api/merchant/products", {
                    "name": name,
                    "tag": random.choice(TAGS),
                    "price": random.choice([19, 39, 59, 99, 129, 199, 299, 399, 599]),
                    "desc": f"{name}（仿真数据，用于压测与画像演示）",
                    "specs": [["型号", f"M{k}"], ["保修", "一年"]],
                }, token=token)
                if d2.get("ok"):
                    products.append(d2["product"])
            self.merchants.append({"id": mid, "token": token, "shop_name": shop,
                                   "products": products})
            log(f"商家 {mid}（{shop}）建好，商品 {len(products)} 件")
        self.products = [p for m in self.merchants for p in m["products"]]
        return len(self.products) > 0

    # ---------- 一个顾客 ----------
    def run_user(self, idx):
        name = f"顾客{self.tag}-{idx:04d}"
        t0 = time.time()
        rec = {"index": idx, "name": name, "id": "", "steps": [], "chat": [],
               "final": {"orders": [], "returns": []}, "ok": True, "errors": []}

        def step(label, ok, detail=""):
            rec["steps"].append({"step": label, "ok": bool(ok), "detail": str(detail)[:200],
                                 "at": datetime.now().strftime("%H:%M:%S")})
            if not ok:
                rec["ok"] = False
                rec["errors"].append(f"{label}：{detail}")

        # 1) 注册
        sex = random.choice(["男", "女", "未知"])
        code, data = self.api.post("/api/register", {
            "name": name, "password": PASSWORD, "password2": PASSWORD,
            "sex": sex, "age": random.randint(18, 55),
        })
        if not data.get("ok"):
            step("注册", False, err(data))
            rec["ms"] = int((time.time() - t0) * 1000)
            return rec
        uid = str(data["user"]["id"])
        rec["id"] = uid
        rec["sex"] = sex
        step("注册", True, f"user_id={uid}")
        me = {"id": uid, "name": name}

        # 2) 存一条收货地址
        prov, city, dist = random.choice(self.region_pairs)
        code, data = self.api.post("/api/addresses", {
            "user": me, "receiver_name": name, "phone": "13" + str(random.randint(10 ** 8, 10 ** 9 - 1)),
            "province": prov, "city": city, "district": dist,
            "detail": f"仿真路{random.randint(1, 999)}号", "is_default": True,
        })
        step("加地址", data.get("ok"), err(data) or f"{prov}{city}{dist}")
        address = f"{prov}{city}{dist}仿真路"

        # 3) 客服咨询（抽样真调 Dify，其余写 MongoDB 模拟）
        product = random.choice(self.products)
        q = random.choice(QUESTIONS)
        mid = str(product.get("owner_id") or "")
        mname = product.get("owner_name") or ""
        use_dify = random.random() < self.args.dify_rate
        if use_dify:
            code, data = self.api.post("/api/consult", {
                "message": q, "merchant_id": mid, "merchant_name": mname,
                "product": product, "user": me,
            }, timeout=180)
            reply = data.get("reply") or ""
            step("客服咨询(真Dify)", data.get("ok"), reply[:60] or err(data))
            rec["chat"].append({"role": "user", "text": q, "source": "dify"})
            if reply:
                rec["chat"].append({"role": "bot", "text": reply, "source": "dify"})
        else:
            # 直接写 MongoDB：与 POST /api/consult 落库的消息格式一致，只是不调 Dify
            from 公共.chat_store import append_message
            reply = random.choice(BOT_REPLIES)
            append_message(mid, uid, name, "user", q, mname)
            append_message(mid, uid, name, "bot", reply, mname)
            step("客服咨询(模拟)", True, reply[:40])
            rec["chat"].append({"role": "user", "text": q, "source": "sim"})
            rec["chat"].append({"role": "bot", "text": reply, "source": "sim"})

        # 4) 下单（1~2 件商品）
        picked = random.sample(self.products, k=min(len(self.products), random.choice([1, 1, 2])))
        items = [{"name": p.get("name"), "qty": 1, "price": p.get("price"),
                  "product_id": p.get("id")} for p in picked]
        code, data = self.api.post("/api/orders", {
            "user": me, "province": prov, "city": city, "district": dist,
            "detail": f"仿真路{random.randint(1, 999)}号",
            "receiver_name": name, "phone": "13" + str(random.randint(10 ** 8, 10 ** 9 - 1)),
            "items": items,
        })
        order_ids = data.get("order_ids") or []
        step("下单", data.get("ok"), ",".join(str(x) for x in order_ids) or err(data))
        if not order_ids:
            rec["ms"] = int((time.time() - t0) * 1000)
            return rec

        # 5) 售后：四种路径，覆盖「未发货即时 / 已发货退款推进 / 已发货退货全链路」
        roll = random.random()
        status_code, orders_data = self.api.get(
            f"/api/orders?user_id={uid}&user_name={requests.utils.quote(name)}")
        my_orders = orders_data.get("orders") or []
        target = next((o for o in my_orders if str(o.get("id")) in
                       [str(x) for x in order_ids]), None) or (my_orders[0] if my_orders else None)

        if target is None:
            step("售后", False, "查不到刚下的订单")
        else:
            oid = str(target.get("id"))
            owner = str(target.get("merchant_id") or mid)
            owner_token = self.merchant_token.get(owner, "")
            if roll < 0.45:
                self._refund_unshipped(me, oid, step)
            elif roll < 0.65:
                self._return_unshipped(me, oid, step)
            elif roll < 0.85:
                self._refund_shipped(me, oid, owner_token, step)
            else:
                self._return_full_chain(me, oid, owner_token, name, step)

        # 6) 收尾：拉最终状态
        code, data = self.api.get(
            f"/api/orders?user_id={uid}&user_name={requests.utils.quote(name)}")
        for o in (data.get("orders") or []):
            rec["final"]["orders"].append({
                "order_id": o.get("id"), "goodsname": o.get("goodsname"),
                "status_text": o.get("status_text"), "waybill": o.get("waybill"),
            })
        code, data = self.api.get(
            f"/api/returns?user_id={uid}&user_name={requests.utils.quote(name)}")
        for r in (data.get("list") or []):
            rec["final"]["returns"].append({
                "return_id": r.get("id"), "type_text": r.get("type_text"),
                "status_text": r.get("status_text"), "order_id": r.get("order_id"),
            })
        rec["ms"] = int((time.time() - t0) * 1000)
        return rec

    # ---------- 四种售后路径 ----------
    def _refund_unshipped(self, me, oid, step):
        """未发货退款：提交即完成（无需商家同意）。"""
        code, data = self.api.post("/api/returns",
                                   {"user": me, "order_id": oid, "return_type": "refund"})
        ret = data.get("return") or {}
        step("未发货退款", data.get("ok"), f"{ret.get('id', '')} {ret.get('status_text', '')}"
             or err(data))

    def _return_unshipped(self, me, oid, step):
        """未发货退货：同样提交即完成。"""
        code, data = self.api.post("/api/returns",
                                   {"user": me, "order_id": oid, "return_type": "return"})
        ret = data.get("return") or {}
        step("未发货退货", data.get("ok"), f"{ret.get('id', '')} {ret.get('status_text', '')}"
             or err(data))

    def _refund_shipped(self, me, oid, token, step):
        """已发货退款：商家多次「推进」直到退款完成。"""
        code, data = self.api.post("/api/merchant/orders/%s/ship" % oid,
                                   {"waybill": self._waybill()}, token=token)
        step("商家发货", data.get("ok"), err(data) or "已发货")

        code, data = self.api.post("/api/returns", {
            "user": me, "order_id": oid, "return_type": "refund",
            "reason": "仿真退款：拍错了",
        })
        if not data.get("ok"):
            step("已发货退款", False, err(data))
            return
        rid = (data.get("return") or {}).get("id")
        final = ""
        for _ in range(8):
            code, d2 = self.api.post(f"/api/merchant/refunds/{rid}/status",
                                     {"status": "next"}, token=token)
            ret = d2.get("refund") or {}
            final = str(ret.get("status") or "")
            if final in ("refunded", "rejected"):
                break
        step("已发货退款", final == "refunded", f"{rid} → {final}")

    def _return_full_chain(self, me, oid, token, name, step):
        """已发货退货全链路：发货 → 签收 → 申请退货 → 商家同意 → 顾客寄回 → 完成。

        依赖仿真期间把 default_delay 调成 1 秒，签收与寄回到仓都能立刻发生。
        """
        code, data = self.api.post("/api/merchant/orders/%s/ship" % oid,
                                   {"waybill": self._waybill()}, token=token)
        step("商家发货", data.get("ok"), err(data) or "已发货")
        time.sleep(max(1.0, self.args.short_delay + 0.6))     # 等物流自动签收

        code, data = self.api.post("/api/returns", {
            "user": me, "order_id": oid, "return_type": "return", "reason": "仿真退货：不合适",
        })
        if not data.get("ok"):
            step("申请退货", False, err(data))
            return
        rid = (data.get("return") or {}).get("id")
        step("申请退货", True, rid)

        code, data = self.api.post(f"/api/merchant/refunds/{rid}/status",
                                   {"status": "next"}, token=token)
        step("商家同意退货", data.get("ok"),
             (data.get("refund") or {}).get("status_text") or err(data))

        code, data = self.api.post(f"/api/returns/{rid}/shipped",
                                   {"user": me, "waybill": self._waybill()})
        step("顾客寄回", data.get("ok"), err(data) or "已确认寄出")

        time.sleep(max(1.0, self.args.short_delay + 0.6))     # 等退货到仓自动完成
        code, data = self.api.get(f"/api/returns/{rid}")
        ret = data.get("return") or {}
        # 状态以 status_text 为准：status 列可能还停在 shipping，文案是实时算出来的
        done = str(ret.get("status") or "") in ("done", "refunded") or \
            str(ret.get("status_text") or "") in ("退换完成", "退款完成")
        step("退货完成", done, ret.get("status_text") or "")

    @staticmethod
    def _waybill():
        return f"{random.randint(1000000, 9999999)}"

    # ---------- 跑起来 ----------
    def run(self):
        out_root = os.path.join(ROOT_DIR, "结果")
        os.makedirs(out_root, exist_ok=True)
        self.out_dir = os.path.join(
            out_root, "仿真_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.join(self.out_dir, "users"), exist_ok=True)

        self.load_regions()
        if not self.make_merchants():
            log("没有可用商品，退出")
            return 1

        log(f"开始并发仿真：{self.args.users} 个顾客，并发 {self.args.concurrency}")
        started = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as pool:
            futures = [pool.submit(self.run_user, i + 1) for i in range(self.args.users)]
            for fut in as_completed(futures):
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"index": -1, "name": "?", "id": "", "steps": [],
                           "chat": [], "final": {}, "ok": False,
                           "errors": [f"线程异常：{e}"], "ms": 0}
                self.results.append(rec)
                done += 1
                if done % 100 == 0 or done == self.args.users:
                    rate = done / max(0.001, time.time() - started)
                    log(f"进度 {done}/{self.args.users}  （{rate:.1f} 用户/秒）")

        self.save()
        log(f"总耗时 {int(time.time() - started)} 秒，结果目录：{self.out_dir}")
        return 0

    # ---------- 落盘 ----------
    def save(self):
        users_dir = os.path.join(self.out_dir, "users")
        chat_lines = []
        csv_rows = []
        for rec in sorted(self.results, key=lambda r: r.get("index") or 0):
            with open(os.path.join(users_dir, f"{rec.get('id') or 'X_' + str(rec.get('index'))}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            for c in rec.get("chat") or []:
                chat_lines.append(json.dumps({
                    "user_id": rec.get("id"), "user_name": rec.get("name"),
                    "role": c.get("role"), "text": c.get("text"),
                    "source": c.get("source"),
                }, ensure_ascii=False))
            csv_rows.append([
                rec.get("id"), rec.get("name"), rec.get("sex"),
                "是" if rec.get("ok") else "否",
                rec.get("ms"),
                " / ".join(f"{s['step']}{'✓' if s['ok'] else '✗'}" for s in rec.get("steps") or []),
                " | ".join(str(x) for x in (rec.get("errors") or [])),
            ])

        with open(os.path.join(self.out_dir, "对话.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(chat_lines) + ("\n" if chat_lines else ""))

        with open(os.path.join(self.out_dir, "users.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["顾客ID", "姓名", "性别", "整体成功", "耗时ms", "步骤", "失败原因"])
            w.writerows(csv_rows)

        failures = [r for r in self.results if not r.get("ok")]
        if failures:
            with open(os.path.join(self.out_dir, "失败清单.txt"), "w", encoding="utf-8") as f:
                for r in failures:
                    f.write(f"{r.get('id')} {r.get('name')}: {' | '.join(r.get('errors') or [])}\n")

        summary = self.build_summary()
        with open(os.path.join(self.out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        log("=" * 56)
        log(f"顾客总数        : {summary['users']}")
        log(f"整体成功率      : {summary['success_rate']}")
        log(f"失败用户        : {summary['failed']}")
        log(f"下单成功        : {summary['orders_ok']}")
        log(f"售后结果分布    : {summary['return_status']}")
        log(f"对话条数        : {summary['chat_messages']}（真调 Dify {summary['dify_calls']} 次）")
        log(f"数据库最终行数  : {summary['db_counts']}")
        log("=" * 56)

    def build_summary(self):
        step_stat = {}
        for r in self.results:
            for s in r.get("steps") or []:
                item = step_stat.setdefault(s["step"], {"ok": 0, "fail": 0})
                item["ok" if s["ok"] else "fail"] += 1

        return_status = {}
        orders_ok = 0
        chat_messages = 0
        dify_calls = 0
        for r in self.results:
            for x in (r.get("final") or {}).get("returns") or []:
                key = str(x.get("status_text") or "未知")
                return_status[key] = return_status.get(key, 0) + 1
            for x in (r.get("final") or {}).get("orders") or []:
                orders_ok += 1
            chat_messages += len(r.get("chat") or [])
            dify_calls += sum(1 for c in (r.get("chat") or []) if c.get("source") == "dify")

        total = len(self.results) or 1
        failed = sum(1 for r in self.results if not r.get("ok"))
        return {
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "配置": {
                "用户数": self.args.users, "并发": self.args.concurrency,
                "商家数": self.args.merchants, "每商家商品数": self.args.products,
                "Dify抽样比例": self.args.dify_rate,
                "物流等待秒数(仿真期间)": None if self.args.no_short_delay else self.args.short_delay,
            },
            "users": total,
            "success_rate": f"{round((total - failed) / total * 100, 2)}%",
            "failed": failed,
            "orders_ok": orders_ok,
            "chat_messages": chat_messages,
            "dify_calls": dify_calls,
            "steps": step_stat,
            "return_status": return_status,
            "db_counts": self.db_counts(),
        }

    @staticmethod
    def db_counts():
        """仿真结束后数据库里的真实行数。"""
        out = {}
        try:
            from 公共.db_config import get_conn
            conn = get_conn()
            with conn.cursor() as cur:
                for t in ["user", "merchant", "order", "return_order", "logistics", "address_book"]:
                    cur.execute(f"SELECT COUNT(*) n FROM `{t}`")
                    out[t] = cur.fetchone()["n"]
            conn.close()
        except Exception as e:
            out["错误"] = str(e)[:80]
        try:
            from 公共.chat_store import get_client
            from 公共.db_config import MONGO_CONFIG
            db = get_client()[MONGO_CONFIG["database"]]
            out["presale_threads"] = db["presale_threads"].count_documents({})
        except Exception as e:
            out["mongo错误"] = str(e)[:80]
        return out


def main():
    p = argparse.ArgumentParser(description="仿真压测：模拟大量用户的完整业务链路")
    p.add_argument("--users", type=int, default=2000, help="顾客数量（默认 2000）")
    p.add_argument("--concurrency", type=int, default=24, help="并发线程数（默认 24）")
    p.add_argument("--merchants", type=int, default=2, help="商家数量（默认 2）")
    p.add_argument("--products", type=int, default=6, help="每个商家的商品数（默认 6）")
    p.add_argument("--dify-rate", type=float, default=0.03, help="客服真调 Dify 的比例（默认 0.03）")
    p.add_argument("--short-delay", type=int, default=1, help="仿真期间物流等待秒数（默认 1）")
    p.add_argument("--no-short-delay", action="store_true",
                   help="不修改 config.json 的物流等待秒数")
    p.add_argument("--base-url", default=DEFAULT_BASE, help="后端地址")
    p.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = p.parse_args()

    if args.concurrency > 60:
        print("并发不要超过 60（MySQL max_connections=151，开太大反而全失败）")
        return 2

    print("=" * 56)
    print(f"仿真规模：{args.users} 顾客 / {args.merchants} 商家 × {args.products} 商品 / "
          f"并发 {args.concurrency}")
    print(f"客服对话：{round(args.dify_rate * 100, 1)}% 真调 Dify，其余写 MongoDB 模拟")
    if not args.no_short_delay:
        print(f"仿真期间会把 config.json 的 default_delay 临时改成 {args.short_delay} 秒（结束自动还原）")
    print(f"后端：{args.base_url}")
    print("=" * 56)
    if not args.yes:
        if input("确认开始？输入 yes 继续：").strip().lower() not in ("y", "yes"):
            print("已取消")
            return 0

    sim = Simulation(args)
    if not sim.check_ready():
        return 1

    original_delay = None
    try:
        if not args.no_short_delay:
            original_delay = read_config().get("default_delay")
            set_default_delay(args.short_delay)
            log(f"物流等待秒数：{original_delay} → {args.short_delay}（结束后还原）")
        return sim.run()
    finally:
        if original_delay is not None:
            set_default_delay(original_delay)
            log(f"已还原物流等待秒数：{original_delay}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)