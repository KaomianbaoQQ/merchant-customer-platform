# -*- coding: utf-8 -*-
"""给 test 商户（laihan）批量上架商品：建商品 -> 初始化 SKU 库存。

直接复用后端同一套模块（公共.store / 公共.inventory），
因此商品结构、SKU 派生规则与商户后台上架完全一致，
跳过 HTTP 鉴权（不需要知道 test 的密码）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 公共 import store, inventory  # noqa: E402

OWNER_ID = "2"
OWNER_NAME = "laihan"

# 商品清单：(id, 名称, 类目, 主价, 描述, 参数, [(规格名, 价格, 库存)])
PRODUCTS = [
    ("LH_LAPTOP14", "星岚 Book Air 14 轻薄本", "数码", 5299,
     "14 英寸 2.8K 高色域屏，全金属机身 1.28kg，双风扇四热管散热，支持 65W PD 快充。",
     [["屏幕", "14 英寸 2.8K 90Hz"], ["重量", "1.28kg"], ["接口", "雷电4 ×2 / USB-A / HDMI"]],
     [("16G+512G 银灰", 5299, 15), ("16G+1T 银灰", 5999, 10), ("32G+1T 深空黑", 7299, 6)]),

    ("LH_MONITOR27", "澄光 27 英寸 2K 显示器", "数码", 1099,
     "27 英寸 IPS 面板，2560×1440 分辨率，出厂校色 ΔE<2，三面窄边，支持 75×75 壁挂。",
     [["尺寸", "27 英寸"], ["分辨率", "2560×1440"], ["色域", "99% sRGB"]],
     [("2K / 75Hz", 1099, 20), ("2K / 165Hz 电竞版", 1499, 12)]),

    ("LH_KEYBOARD87", "静音 87 键机械键盘", "数码", 399,
     "87 键紧凑布局，Gasket 结构多层消音，全键热插拔，三模连接（有线 / 蓝牙 / 2.4G）。",
     [["配列", "87 键"], ["轴体", "静音红轴"], ["连接", "三模"]],
     [("奶油白 / 静音红轴", 399, 25), ("夜空灰 / 静音茶轴", 419, 20), ("奶油白 / 静音红轴 套装", 469, 10)]),

    ("LH_TSHIRT", "云致 纯棉圆领短袖 T 恤", "服饰", 129,
     "新疆长绒棉 32 支双面针织，克重 190g，领口双层罗纹不易变形，男女同款宽松版型。",
     [["面料", "100% 长绒棉"], ["克重", "190g"], ["版型", "宽松直筒"]],
     [("白色 / M", 129, 40), ("白色 / L", 129, 45), ("黑色 / L", 129, 42), ("黑色 / XL", 129, 30)]),

    ("LH_BACKPACK", "轻野 城市通勤双肩包", "箱包", 329,
     "20L 容量，可放 16 英寸笔记本，防泼水面料，背部透气蜂窝，隐藏式防盗口袋。",
     [["容量", "20L"], ["适配", "16 英寸笔记本"], ["面料", "防泼水尼龙"]],
     [("曜石黑", 329, 22), ("雾霾灰", 329, 18)]),

    ("LH_COFFEE", "山见 手冲挂耳咖啡 10 包", "食品", 69,
     "云南与埃塞俄比亚豆拼配，下单后现磨充氮锁鲜，冷萃热冲皆宜，单包 10g。",
     [["规格", "10g × 10 包"], ["保质期", "9 个月"], ["冲泡", "冷萃 / 热冲"]],
     [("中度烘焙", 69, 60), ("深度烘焙", 69, 55), ("混合装（各 5 包）", 79, 40)]),

    ("LH_FACEWASH", "净研 氨基酸洁面乳", "美妆个护", 89,
     "椰油酰甘氨酸钾配方，pH 5.5 弱酸性，无皂基无香精，敏感肌可用，120g。",
     [["净含量", "120g"], ["pH", "5.5"], ["肤质", "敏感肌适用"]],
     [("120g 单支", 89, 50), ("120g × 2 支装", 159, 25)]),

    ("LH_BEDSHEET", "舒棉 60 支长绒棉四件套", "家居", 599,
     "60 支贡缎长绒棉，活性印染不褪色，被套四角固定带，含被套 / 床单 / 枕套 ×2。",
     [["材质", "60 支长绒棉"], ["工艺", "贡缎活性印染"], ["件数", "四件套"]],
     [("1.5m 床 / 月白", 549, 15), ("1.8m 床 / 月白", 599, 18), ("2.0m 床 / 雾蓝", 649, 10)]),

    ("LH_TUMBLER", "恒温 316 不锈钢保温杯", "家居", 129,
     "316 食品级不锈钢内胆，真空双层锁温，24 小时保冷 / 12 小时保温，一键弹盖单手可开。",
     [["容量", "450ml"], ["材质", "316 不锈钢"], ["保温", "12 小时 ≥55℃"]],
     [("450ml / 雾白", 129, 35), ("600ml / 曜黑", 149, 28)]),

    ("LH_NOTEBOOK", "晨读 A5 横线笔记本", "文具", 29,
     "100g 米黄道林纸不洇墨，180° 平摊锁线装订，A5 尺寸 148×210mm，共 160 页。",
     [["尺寸", "A5"], ["页数", "160 页"], ["纸张", "100g 米黄"]],
     [("单本 / 燕麦色", 29, 100), ("三本装（三色）", 79, 45)]),

    ("LH_TREKKING", "走野 超轻碳素登山杖", "运动户外", 159,
     "三节外锁快扣，单支仅 210g，7075 铝合金杖身 + 碳纤维杖尖，配雪托与收纳袋。",
     [["单支重量", "210g"], ["长度", "65-135cm 可调"], ["材质", "7075 铝合金"]],
     [("单支 / 荧光橙", 159, 30), ("一对 / 荧光橙", 299, 15)]),

    ("LH_BIB", "暖芽 婴儿纯棉口水巾 5 条装", "母婴", 39,
     "A 类婴幼儿标准纯棉纱布，六层加厚吸水，按扣可调节，机洗不掉絮。",
     [["材质", "A 类纯棉纱布"], ["层数", "六层"], ["数量", "5 条"]],
     [("小鹿印花 5 条", 39, 80), ("云朵印花 5 条", 39, 75)]),
]


def main():
    existing = {p.get("id") for p in store.list_products(include_offline=True)}
    created, skipped = 0, 0
    for pid, name, tag, price, desc, specs, skus in PRODUCTS:
        if pid in existing:
            print(f"跳过（已存在）: {pid} {name}")
            skipped += 1
            continue
        product = store.normalize_product({
            "id": pid, "name": name, "tag": tag, "price": price,
            "desc": desc, "specs": specs,
            "skus": [{"name": n, "price": p} for n, p, _ in skus],
            "owner_id": OWNER_ID, "owner_name": OWNER_NAME,
            "images": [],
        })
        product["owner_id"] = OWNER_ID
        product["owner_name"] = OWNER_NAME
        product = store.create_product(product)

        stocks = {}
        for d in inventory.sku_definitions(product):
            for n, p, qty in skus:
                if d["name"] == n:
                    stocks[d["sku_id"]] = qty
        ok, msg, defs = inventory.sync_skus(product, stocks)
        print(f"{'✓' if ok else '✗'} {pid} {name}  SKU {len(defs)} 个 {msg if not ok else ''}")
        created += 1

    # 复核
    print("\n=== test 商户商品总览 ===")
    rows = inventory.stock_rows()
    by_sku = {sid: r for pid_rows in rows.values() for sid, r in pid_rows.items()}
    total = 0
    for p in store.list_products(owner_id=OWNER_ID, include_offline=True):
        total += 1
        parts = []
        for d in p.get("skus") or []:
            r = by_sku.get(d["sku_id"]) or {}
            parts.append(f"{d['name']} ¥{d['price']} 存{r.get('stock', 0)}")
        print(f"{total:2d}. [{p.get('tag')}] {p['name']} —— " + " / ".join(parts))
    print(f"\n共 {created} 个新上架，跳过 {skipped} 个；test 商户当前商品总数 {total}。")


if __name__ == "__main__":
    main()
