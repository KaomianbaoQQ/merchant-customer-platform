import json

import requests

from 公共.app_config import dify_workflow

_CFG = dify_workflow("after_sales")
API_KEY = _CFG["api_key"]
DIFY_BASE_URL = _CFG["base_url"]
WORKFLOW_RUN_URL = f"{DIFY_BASE_URL}/workflows/run"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

BIZ_KEYS = ("ops_json", "json")

def _pick_biz_payload(outputs):
    """
    从 Dify 的 outputs 里挑出业务操作字段。
    优先 ops_json / json；都没有时兜底扫描任何含 ops 或 operate 的 dict。
    返回 (字段名, 原始值)，找不到返回 (None, None)。
    """
    for key in BIZ_KEYS:
        if key in outputs and outputs[key] not in (None, "", {}):
            return key, outputs[key]

    for key, value in outputs.items():
        if isinstance(value, dict) and ("ops" in value or "operate" in value):
            return key, value
    return None, None

def _resp_text(resp):
    try:
        return resp.text
    except Exception:
        return "无"

def run_dify_workflow(user_input: str, user_id: str, user_role: str, user_name: str,
                      user_orders: str = "") -> dict:
    """
    调用 Dify 工作流。

    统一返回 dict（不再依赖元组位置，避免格式变化时静默丢数据）：
        ok            bool          工作流是否执行成功
        status        str|None      Dify 的 status
        error_type    str|None
        error_message str|None
        biz_raw       dict|list|str|None  业务字段原始内容（未解析，可能是 JSON 也可能是纯文本）
        biz_source    str|None      该内容来自哪个输出字段（json / ops_json / ...）
        talk          str|None      闲聊分支
        comfort       str|None      安慰分支
        policy        str|None      退换货政策咨询分支（知识库检索）
        aftersale     str|None      售后进度 / 仲裁查询分支（依据退换货记录作答）
        return_pick   str|None      退换货订单定位分支：JSON 文本
                                    {"order_id","quantity","confidence","reason"}
        node_error_message str|None  失败分支返回的错误信息
        node_error_type    str|None  失败分支返回的错误类型
        output_keys   list          本次 Dify 返回的所有输出字段名
        raw           dict|None     完整原始返回
    """
    result = {
        "ok": False,
        "status": None,
        "error_type": None,
        "error_message": None,
        "biz_raw": None,
        "biz_source": None,
        "talk": None,
        "comfort": None,
        "policy": None,
        "aftersale": None,
        "return_pick": None,
        "node_error_message": None,
        "node_error_type": None,
        "output_keys": [],
        "raw": None,
    }

    payload = {
        "inputs": {
            "user_input": user_input,
            "user_id": user_id,
            "user_role": user_role,
            "user_name": user_name,
            # 该用户已有订单（文本），供 LLM 解析"我的订单"这类模糊说法
            "user_orders": user_orders,
        },
        "response_mode": "blocking",
        "user": "python_client",
    }

    resp = None
    try:
        resp = requests.post(
            WORKFLOW_RUN_URL,
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()

        print("\n====【DEBUG Dify完整原始返回】====")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        result["raw"] = body

        data = body.get("data", {}) or {}
        status = data.get("status")
        result["status"] = status
        result["error_type"] = data.get("error_type")
        result["error_message"] = data.get("error_message")

        if status != "succeeded":
            print("[DIFY ERROR] 工作流执行失败")
            print(f"error_type: {result['error_type']}")
            print(f"error_message: {result['error_message']}")
            return result

        outputs = data.get("outputs", {}) or {}
        result["output_keys"] = list(outputs.keys())
        print(f"\n====【DEBUG】识别到的输出keys: {result['output_keys']}")

        biz_source, biz_raw = _pick_biz_payload(outputs)
        result["biz_source"] = biz_source
        result["biz_raw"] = biz_raw
        result["talk"] = outputs.get("talk_output")
        result["comfort"] = outputs.get("comfort")
        # 退货 / 换货 / 退款政策咨询分支（知识库检索后作答）的输出
        result["policy"] = outputs.get("policy_output")
        # 售后进度 / 仲裁查询分支（依据入参里的退换货记录作答）的输出
        result["aftersale"] = outputs.get("aftersale_output")
        # 退换货订单定位分支（LLM 语义判断用户说的是哪一笔订单）的输出
        result["return_pick"] = outputs.get("return_pick")
        result["node_error_message"] = outputs.get("error_message")
        result["node_error_type"] = outputs.get("error_type")
        result["ok"] = True

        print(f"====【DEBUG】业务字段来源: {biz_source}，类型: {type(biz_raw).__name__}")
        return result

    except requests.exceptions.ConnectionError:
        print("[ERROR] 连接Dify失败，请确认Dify服务是否启动，地址http://localhost/v1是否可访问")
        result["error_message"] = "连接Dify失败"
        return result
    except requests.exceptions.HTTPError as e:
        print(f"[HTTP ERROR] 请求异常: {e}")
        print(f"响应内容: {_resp_text(resp)}")
        result["error_message"] = f"HTTP错误: {e}"
        return result
    except json.JSONDecodeError:
        print("[ERROR] Dify返回不是合法JSON")
        print(f"原始响应: {_resp_text(resp)}")
        result["error_message"] = "Dify返回不是合法JSON"
        return result
    except Exception as e:
        print(f"[UNEXPECTED ERROR] 未知异常: {str(e)}")
        result["error_message"] = f"未知异常: {e}"
        return result
