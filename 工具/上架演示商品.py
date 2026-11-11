# -*- coding: utf-8 -*-
"""上架演示商品：注册/登录商户 -> 建商品（带 SKU）-> 设置库存。
仅用标准库，直连本地后端 HTTP 接口。
"""
import json
import urllib.request
import urllib.error

B = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(path, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(B + path, data=body, method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        r = OPENER.open(req)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}

USERNAME = "demo_shop"
PASSWORD = "demo123456"

st, body = call("/api/merchant/login", {"username": USERNAME, "password": PASSWORD})
if st != 200:
    st, body = call("/api/merchant/register", {
        "username": USERNAME, "password": PASSWORD, "password2": PASSWORD,
        "name": "演示商户", "shop_name": "Kaomianbao 官方旗舰店",
    })
    print("register:", st, body.get("message") or body.get("error"))
    st, body = call("/api/merchant/login", {"username": USERNAME, "password": PASSWORD})

token = body.get("token")
print("merchant login:", st, "token_len=", len(token or ""))
mid = (body.get("merchant") or {}).get("id")
print("merchant id:", mid)

PRODUCTS = [
    {
        "id": "DEMO_PHONE", "name": "星海 X1 智能手机", "tag": "数码",
        "price": 3299, "desc": "6.7 英寸 OLED 屏，5000mAh 电池，支持 5G 双卡双待。",
        "specs": [["屏幕", "6.7 英寸 OLED"], ["电池", "5000mAh"], ["存储", "256GB"]],
        "skus": [
            {"sku_id": "DEMO_PHONE_BLK128", "name": "曜石黑 / 128GB", "price": 2999},
            {"sku_id": "DEMO_PHONE_BLK256", "name": "曜石黑 / 256GB", "price": 3299},
            {"sku_id": "DEMO_PHONE_BLU256", "name": "晴空蓝 / 256GB", "price": 3399},
        ],
    },
    {
        "id": "DEMO_EARBUDS", "name": "澄音 Pro 降噪耳机", "tag": "数码",
        "price": 899, "desc": "主动降噪 42dB，单次续航 8 小时，支持无线充电。",
        "specs": [["降噪", "42dB"], ["续航", "8 小时"], ["充电", "无线 + Type-C"]],
        "skus": [
            {"sku_id": "DEMO_EARBUDS_WHT", "name": "云白", "price": 899},
            {"sku_id": "DEMO_EARBUDS_BLK", "name": "墨黑", "price": 899},
        ],
    },
    {
        "id": "DEMO_MUG", "name": "陶白陶瓷马克杯", "tag": "家居",
        "price": 79, "desc": "380ml 容量，釉面细腻，可微波加热、可进洗碗机。",
        "specs": [["容量", "380ml"], ["材质", "陶瓷"], ["适用", "微波炉 / 洗碗机"]],
        "skus": [
            {"sku_id": "DEMO_MUG_380", "name": "380ml / 米白", "price": 79},
            {"sku_id": "DEMO_MUG_500", "name": "500ml / 米白", "price": 99},
        ],
    },
]

created = []
for p in PRODUCTS:
    st, body = call("/api/merchant/products", p, token=token)
    if st in (200, 201):
        prod = body.get("product") or {}
        created.append(prod)
        print(f"create {p['id']}: ok, skus={[s['sku_id'] for s in prod.get('skus', [])]}")
    elif st == 400 and "已存在" in str(body):
        print(f"create {p['id']}: already exists")
        created.append({"id": p["id"]})
    else:
        print(f"create {p['id']}: FAIL", st, body)

STOCK = {
    "DEMO_PHONE":   {"DEMO_PHONE_BLK128": 8, "DEMO_PHONE_BLK256": 12, "DEMO_PHONE_BLU256": 5},
    "DEMO_EARBUDS": {"DEMO_EARBUDS_WHT": 20, "DEMO_EARBUDS_BLK": 20},
    "DEMO_MUG":     {"DEMO_MUG_380": 30, "DEMO_MUG_500": 15},
}
for pid, stocks in STOCK.items():
    st, body = call(f"/api/merchant/inventory/{pid}", {"stocks": stocks}, token=token)
    print(f"stock {pid}:", st, "ok=", body.get("ok"))

# 复核：商户视角库存
st, body = call("/api/merchant/inventory", token=token)
for p in body.get("products", []):
    row = ", ".join(f"{s['sku_id']}[存{s['stock']}/锁{s.get('locked', 0)}/售{s.get('sold', 0)}]"
                    for s in p.get("skus", []))
    print("  inv:", p["id"], "->", row)

# 客户端视角
st, body = call("/api/products")
print("client products:", len(body.get("products") or []))
for p in body.get("products") or []:
    print("   ", p["id"], p["name"], p["price"], "skus=", len(p.get("skus") or []))
