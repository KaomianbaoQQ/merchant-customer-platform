# -*- coding: utf-8 -*-
"""全国行政区划数据 —— 下单收货地址必须从中选择省 / 市 / 区。

数据源：工具/全国行政区划数据.xlsx（三列：省、地级市、县/区，2880 行）。
启动时从 Excel 批量导入到数据库 region 表，之后所有校验走数据库缓存。

对外接口：
    ensure_region_table()                 -> 建表并从 Excel 导入数据
    load_regions()                        -> {省: {市: [区, ...]}}
    list_provinces()                      -> [省, ...]
    list_cities(province)                 -> [市, ...]
    list_districts(province, city)        -> [区, ...]
    validate_region(province, city, district) -> (是否有效, 提示)
"""
import os
import threading

from openpyxl import load_workbook
from .db_config import get_conn

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCEL_PATH = os.path.join(ROOT_DIR, "工具", "全国行政区划数据.xlsx")

_lock = threading.Lock()
_cache = None  # {省: {市: [区, ...]}}，None 表示尚未加载

CREATE_REGION_TABLE = """
CREATE TABLE IF NOT EXISTS `region` (
    `id`         INT AUTO_INCREMENT PRIMARY KEY COMMENT '自增ID',
    `province`   VARCHAR(64) NOT NULL COMMENT '省份',
    `city`       VARCHAR(64) NOT NULL COMMENT '地级市',
    `district`   VARCHAR(64) NOT NULL COMMENT '县/区',
    UNIQUE KEY `uk_region` (`province`, `city`, `district`),
    KEY `idx_province` (`province`),
    KEY `idx_city` (`city`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全国行政区划表';
"""


def ensure_region_table():
    """建表并从 Excel 批量导入数据；失败返回 (是否成功, 提示信息)。"""
    try:
        conn = get_conn()
    except Exception as e:
        return False, f"数据库连接失败：{e}"
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_REGION_TABLE)
            # 检查表是否为空，空则导入
            cur.execute("SELECT COUNT(*) AS cnt FROM `region`")
            cnt = (cur.fetchone() or {}).get("cnt", 0)
            if cnt == 0:
                if not os.path.exists(EXCEL_PATH):
                    return False, f"行政区划 Excel 文件不存在：{EXCEL_PATH}"
                wb = load_workbook(EXCEL_PATH, read_only=True, data_only=True)
                ws = wb[wb.sheetnames[0]]
                rows = ws.iter_rows(values_only=True)
                next(rows, None)  # 跳过表头：省 / 地级市 / 县(区)
                inserted = 0
                for row in rows:
                    if not row or len(row) < 3:
                        continue
                    province = str(row[0] or "").strip()
                    city = str(row[1] or "").strip()
                    district = str(row[2] or "").strip()
                    if not (province and city and district):
                        continue
                    try:
                        cur.execute(
                            "INSERT INTO `region` (province, city, district) VALUES (%s, %s, %s)",
                            (province, city, district),
                        )
                        inserted += 1
                    except Exception:
                        pass  # 忽略重复
                wb.close()
                conn.commit()
                print(f"[行政区划] 已导入 {inserted} 条记录到 region 表")
        return True, "region 表已就绪"
    except Exception as e:
        conn.rollback()
        return False, f"行政区划表初始化失败：{e}"
    finally:
        conn.close()


def load_regions():
    """从数据库读取并缓存；首次访问时自动建表导入。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        # 确保表存在且有数据
        ensure_region_table()
        data = {}
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT province, city, district FROM `region` ORDER BY province, city, district")
                for row in cur.fetchall():
                    p = str(row.get("province") or "").strip()
                    c = str(row.get("city") or "").strip()
                    d = str(row.get("district") or "").strip()
                    if not (p and c and d):
                        continue
                    data.setdefault(p, {}).setdefault(c, [])
                    if d not in data[p][c]:
                        data[p][c].append(d)
            conn.close()
        except Exception as e:
            print(f"[行政区划] 读取数据库失败：{e}")
        _cache = data
        return _cache


def list_provinces():
    return list(load_regions().keys())


def list_cities(province):
    return list((load_regions().get(str(province or "").strip()) or {}).keys())


def list_districts(province, city):
    return list(
        (load_regions().get(str(province or "").strip()) or {})
        .get(str(city or "").strip()) or []
    )


def validate_region(province, city, district):
    """校验省 / 市 / 区是否为行政区划数据中的合法组合。

    返回 (是否有效, 提示)。
    """
    regions = load_regions()
    if not regions:
        return False, "行政区划数据未加载，无法校验收货地址"
    p = str(province or "").strip()
    c = str(city or "").strip()
    d = str(district or "").strip()
    if not p or p not in regions:
        return False, "请选择收货地址所在的省份"
    cities = regions[p]
    if not c or c not in cities:
        return False, "请选择收货地址所在的城市"
    if not d or d not in cities[c]:
        return False, "请选择收货地址所在的区/县"
    return True, ""
