"""退换货单号生成 + 送达计时。

提供：
    load_default_delay()    读取 config.json 里的默认等待秒数（默认 10 秒）
    generate_7digit()       根据用户ID、地址、当前时间生成 7 位运单号
    delivery_timer()        子线程：计时，自动计算送达时间，到期打印
    start_delivery_timer()  开启后台计时线程

说明：普通订单的运单号已改为【由商家在商户后台填写】，不再自动生成；
本文件的 generate_7digit 目前仅供退换货流程生成退货/换货单号使用。
本文件原源码缺失（仓库内只残留 __pycache__/快递运单号生成.cpython-314.pyc），
已按字节码 1:1 还原。
"""
import time
import threading
import json
from pathlib import Path

CONFIG_PATH = Path("config.json")


def load_default_delay() -> int:
    """读取配置文件里的默认等待秒数，不存在则默认10秒"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("default_delay", 10)
    return 10


def generate_7digit(user_id: int, address: str) -> int:
    """根据用户ID、地址、当前时间生成7位运单号"""
    ts = int(time.time())
    raw_hash = hash(f"{user_id}{address}{ts}")
    seven_num = abs(raw_hash) % 10000000
    return seven_num


def delivery_timer(delay_second: int, waybill: str):
    """子线程：计时，自动计算送达时间，到期打印"""
    start_ts = time.time()
    arrive_ts = start_ts + delay_second
    arrive_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(arrive_ts))
    print(f"[计时线程] 运单号{waybill}，预计送达时间：{arrive_time_str}，等待{delay_second}秒")
    time.sleep(delay_second)
    print(f"\n【送达】运单号{waybill}，送达时间{arrive_time_str} ✅")


def start_delivery_timer(delay_second: int, waybill: str):
    t = threading.Thread(target=delivery_timer, args=(delay_second, waybill))
    t.start()
    return t
