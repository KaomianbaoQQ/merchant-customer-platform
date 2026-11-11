"""商品咨询（在线客服）—— 调用 Dify「商品咨询」工作流。

工作流地址（用户提供）：http://localhost/workflow/7dQop2ukfclvo8ra
API Key：见 config.json → dify.workflows.consult.api_key（不写进代码）

说明：Dify 的 workflow 网页地址不能直接 POST，真正执行工作流的是
      http://localhost/v1/workflows/run（同一个工作流，用上面的 API Key 鉴权）。

工作流入参：user_input
工作流出参：one / two / three（分别是「单商品咨询」「多商品对比」「闲聊」三个分支）
            four（物流查询分支，见下）/ error_message（节点异常）

「物流查询」分支：Dify 图里新增一个分支，判到物流问法时引用 user_input 里的
【当前商品订单与物流】段落作答。后端在 user_input 里拼好的这段上下文由
build_product_prompt(..., orders=...) 生成，工作流不需要自己查库。
"""
import re
import json

import requests

from 公共.app_config import dify_workflow

# （或环境变量 KB_DIFY_CONSULT_KEY），代码里不写明文。
_CFG = dify_workflow("consult")
CONSULT_API_KEY = _CFG["api_key"]
CONSULT_BASE_URL = _CFG["base_url"]
CONSULT_RUN_URL = f"{CONSULT_BASE_URL}/workflows/run"

# 四个业务分支的输出字段（four = 物流查询）
BRANCH_KEYS = ("one", "two", "three", "four")
BRANCH_NAMES = {"one": "单商品咨询", "two": "多商品对比", "three": "闲聊", "four": "物流查询"}

_HEADERS = {
    "Authorization": f"Bearer {CONSULT_API_KEY}",
    "Content-Type": "application/json",
}

_THINK_RE = re.compile(r"<think[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
# 使用负向先行断言 (?!.*?</think>)，确保不会误删有效内容
_THINK_RE_BACKUP = re.compile(r"<think[^>]*>(?!.*?</think>).*$", re.DOTALL | re.IGNORECASE)

def _clean(text):
    if not isinstance(text, str):
        return ""
    # 第一次清理：移除所有完整闭合的 <think>...</think>
    text = _THINK_RE.sub("", text)
    text = _THINK_RE_BACKUP.sub("", text)
    return text.strip()

def build_product_prompt(message, product=None, conversation_history=None, orders=None,
                         merchant_id=None, merchant_name=None):
    """
    把「当前商家」「当前正在浏览的商品」「当前商品的订单与物流」「最近对话上下文」拼进 user_input，
    便于知识库检索与 LLM 作答（有助于 Dify 判断意图 / 连贯回答，并把回答限定在本商家内）。

    merchant_id / merchant_name: 当前会话对接的商家，用于声明作答范围（同一工作流的检索节点
                                 可用它做按商家的元数据过滤）。
    product: {"name","tag","price","desc","specs":[[k,v],...]}
    conversation_history: [{"role","text"}, ...]，role ∈ user/assistant/bot。
    orders: [{"id","waybill","status_text","create_time","estimated_arrival",...}, ...]
            该顾客在这件商品上的订单，供 Dify「物流查询」分支直接引用（不用自己查库）。
    """
    message = (message or "").strip()
    parts = []

    if merchant_id or merchant_name:
        label = str(merchant_name or "").strip() or "本店"
        parts.append(
            f"【当前商家】{label}（商家ID：{str(merchant_id or '').strip() or '—'}）\n"
            "本会话只服务这一家店铺：请只依据该店铺的商品资料作答，"
            "不要引用或推荐其它店铺的商品、价格与参数。"
        )

    if product:
        name = product.get("name") or ""
        tag = product.get("tag") or ""
        price = product.get("price")
        desc = product.get("desc") or ""
        specs = product.get("specs") or []
        lines = [f"【当前咨询商品】{name}（{tag}，价格 ¥{price}）"]
        if desc:
            lines.append(f"商品简介：{desc}")
        if specs:
            # 取前 8 条关键参数，避免 prompt 过长
            picked = "；".join(f"{k}：{v}" for k, v in specs[:8])
            lines.append(f"关键参数：{picked}")
        parts.append("\n".join(lines))

    if orders:
        lines = ["【当前商品订单与物流】（该顾客在本商品的订单，物流状态由系统实时计算）"]
        for o in orders[:5]:
            seg = [f"订单号 {o.get('id')}",
                   f"下单 {o.get('create_time') or '—'}",
                   f"物流状态 {o.get('status_text') or '—'}"]
            if o.get("waybill"):
                seg.append(f"运单号 {o['waybill']}")
            if o.get("shipped_at"):
                seg.append(f"发货 {o['shipped_at']}")
            if o.get("estimated_arrival"):
                seg.append(f"预计送达 {o['estimated_arrival']}")
            lines.append("；".join(seg))
        parts.append("\n".join(lines))

    if conversation_history:
        recent = list(conversation_history)[-10:]
        hist_lines = []
        for m in recent:
            role = m.get("role") or ""
            txt = str(m.get("text") or "").strip()
            if not txt:
                continue
            who = "用户" if role in ("user",) else "客服"
            hist_lines.append(f"{who}：{txt}")
        if hist_lines:
            parts.append("【对话上下文（最近消息）】")
            parts.extend(hist_lines)

    parts.append(f"用户问题：{message}")
    return "\n".join(parts)

def ask_product_service(user_input, product=None, user_id="web", conversation_history=None,
                        orders=None, merchant_id=None, merchant_name=None):
    """
    调用商品咨询工作流。

    merchant_id / merchant_name 会作为工作流入参一并传过去（工作流「开始」节点需要
    声明这两个变量，检索节点可用它做按商家的元数据过滤 → 客服按商家真正隔离）。

    返回 dict：
        ok         bool
        reply      str            最终回复（已去掉 think、空则给兜底话术）
        branch     str|None       命中的分支字段 one/two/three/four
        branch_name str|None      分支中文名
        outputs    dict           原始 outputs
        error      str|None
    """
    prompt = build_product_prompt(user_input, product, conversation_history, orders,
                                  merchant_id=merchant_id, merchant_name=merchant_name)
    payload = {
        "inputs": {
            "user_input": prompt,
            # 当前商家（未指定时传空串，工作流按「不过滤」处理）
            "merchant_id": str(merchant_id or "").strip(),
            "merchant_name": str(merchant_name or "").strip(),
        },
        "response_mode": "blocking",
        "user": str(user_id or "web"),
    }

    result = {"ok": False, "reply": "", "branch": None, "branch_name": None,
              "outputs": {}, "error": None}

    try:
        resp = requests.post(CONSULT_RUN_URL, headers=_HEADERS, json=payload, timeout=120)
        resp.raise_for_status()
        body = resp.json()
    except requests.exceptions.ConnectionError:
        result["error"] = "连接商品咨询工作流失败，请确认 Dify 服务是否启动"
        result["reply"] = "抱歉，客服系统暂时无法连接，请稍后再试。"
        return result
    except requests.exceptions.HTTPError as e:
        result["error"] = f"HTTP 错误：{e}"
        result["reply"] = "抱歉，客服系统暂时不可用，请稍后再试。"
        return result
    except (ValueError, json.JSONDecodeError):
        result["error"] = "商品咨询工作流返回的不是合法 JSON"
        result["reply"] = "抱歉，客服返回异常，请稍后再试。"
        return result

    data = body.get("data", {}) or {}
    if data.get("status") != "succeeded":
        result["error"] = data.get("error_message") or "工作流执行失败"
        result["reply"] = "抱歉，客服暂时无法回答，请稍后再试。"
        return result

    outputs = data.get("outputs", {}) or {}
    result["outputs"] = outputs

    for key in BRANCH_KEYS:
        cleaned = _clean(outputs.get(key))
        if cleaned:
            result["ok"] = True
            result["branch"] = key
            result["branch_name"] = BRANCH_NAMES.get(key)
            result["reply"] = cleaned
            return result

    err = _clean(outputs.get("error_message"))
    result["error"] = err or "无有效回答"
    result["reply"] = "抱歉，我暂时没有找到相关商品信息，可以换个说法再问我一次吗？"
    return result
