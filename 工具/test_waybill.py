"""运单号生成功能测试（原 test_快递运单号生成.py）。

运行（在项目根目录）：
    .venv/bin/python 工具/test_waybill.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from 物流.waybill import generate_7digit, start_delivery_timer, load_default_delay


def main():
    default_delay = load_default_delay()
    print(f"====快递订单系统=== 默认等待秒数：{default_delay}秒")
    while True:
        print("\n-----新建订单-----")
        try:
            user_id = int(input("用户ID（输入-1退出）："))
            if user_id == -1:
                print("程序结束")
                break
            address = input("收货地址：")
            inp = input(f"等待秒数，直接回车使用默认值({default_delay})：")
            if inp.strip() == "":
                wait_sec = default_delay
            else:
                wait_sec = int(inp)
        except ValueError:
            print("输入错误，请输入数字！")
            continue

        waybill = generate_7digit(user_id, address)
        waybill_str = f"{waybill:07d}"
        print(f"生成7位运单号: {waybill_str}")
        start_delivery_timer(wait_sec, waybill_str)
        print("主线程可继续录入新订单")


if __name__ == "__main__":
    main()
