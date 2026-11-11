import json
from pathlib import Path

CONFIG_PATH = Path("config.json")

def main():
    print("==== 修改默认送达等待时间 ====")
    try:
        new_delay = int(input("请输入新的默认等待秒数："))
        if new_delay <=0:
            print("秒数必须大于0！")
            return
        cfg = {"default_delay": new_delay}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg,f,ensure_ascii=False,indent=2)
        print(f"成功！默认等待时间已设置为 {new_delay} 秒，保存到 config.json")
    except ValueError:
        print("输入不是有效数字！")

if __name__ == "__main__":
    main()
