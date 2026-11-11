"""订单定位 —— 命中判定统一走 Dify。

用户消息里指的是哪一笔订单（商品名语义匹配 / 订单号 / 运单号），
统一调用独立的 Dify 工作流「订单定位」来判断；本地不再用英文 token、覆盖率阈值
这类硬编码规则做命中判定。

对外主函数：
    locate(user_input, user_orders) -> dict | None
"""
import json
import re

import requests

from 公共.app_config import dify_workflow

_CFG = dify_workflow("order_locate")
API_KEY = _CFG["api_key"]
BASE_URL = _CFG["base_url"]
RUN_URL = f"{BASE_URL}/workflows/run"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

def parse_pick(raw):
    """解析「订单定位」输出的 JSON（容忍 LLM 偶尔包一层 ```json 代码块）。"""
    if not raw:
        return None
    data = raw
    if isinstance(data, str):
        text = re.sub(r"```(?:json)?", "", data).strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None

def locate(user_input, user_orders, timeout=60):
    """
    调用「订单定位」工作流，返回 {"order_id","quantity","confidence","reason"} 或 None。

    order_id 为 None 表示 Dify 判定「无法确定唯一一笔」；
    整体返回 None 表示 Dify 不可用或返回异常，由调用方决定兜底。
    """
    payload = {
        "inputs": {"user_input": user_input, "user_orders": user_orders},
        "response_mode": "blocking",
        "user": "python_client",
    }
    try:
        resp = requests.post(RUN_URL, headers=HEADERS, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        if data.get("status") != "succeeded":
            print(f"[订单定位] 工作流未成功：{data.get('error_type')} {data.get('error_message')}")
            return None
        outputs = data.get("outputs") or {}
        pick = parse_pick(outputs.get("order_pick"))
        print(f"[订单定位] Dify 判定：{json.dumps(pick, ensure_ascii=False)}")
        return pick
    except Exception as e:
        print(f"[订单定位] 调用 Dify 失败：{e}")
        return None