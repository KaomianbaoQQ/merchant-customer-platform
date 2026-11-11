"""真实快递查询 —— uapis.cn 快递物流查询接口。

本系统内部运单号是「7 位数字」（商家发货时填写、退换货由平台生成），
走本地 cs.logistics 表 + 送达时间模拟；

**不是 7 位的运单号一律视为外部真实快递单号**，改调 uapis.cn 的真实接口，
拿到快递公司识别结果与完整物流轨迹。

接口（见 https://uapis.cn/openapi.json）：
    GET https://uapis.cn/api/v1/misc/tracking/query
        tracking_number  必填，快递单号
        carrier_code     选填，快递公司编码（不填自动识别）
        phone            选填，收件人手机尾号 4 位（顺丰等必须）
        refresh          选填，是否重新拉取
    返回：tracking_number / carrier_code / carrier_name / status / status_code /
          is_completed / completed_at / track_count / tracks[{time, context}]

对外主函数：
    is_external_waybill(code)          是否为外部（非 7 位）运单号
    query(tracking_number, phone="")   -> (是否成功, 提示信息, 结果 dict 或 None)
"""
import json
import os
import re

import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

DEFAULT_BASE_URL = "https://uapis.cn/api/v1"
# API Key 不写进代码：优先环境变量，其次 config.json 的 express_tracking.api_key
# （config.json 已被 .gitignore 排除，避免密钥进版本库）
DEFAULT_API_KEY = os.environ.get("KB_EXPRESS_API_KEY", "")
# 实测：即使查不到数据，一次请求也要 18~20 秒（官方文档称 1-2 秒，实际偏慢），
# 因此默认超时给到 30 秒，并可在 config.json 里调整。
DEFAULT_TIMEOUT = 30

QUERY_PATH = "/misc/tracking/query"

# 系统内部运单号：7 位纯数字
INTERNAL_WAYBILL_RE = re.compile(r"^\d{7}$")

# uapis 的 status_code → 本系统 5 档进度（0 待发货 / 1 已发货 / 2 运输中 / 3 派送中 / 4 已签收）
STATUS_STEP = {
    "pending": 0,
    "picked_up": 1,
    "in_transit": 2,
    "out_for_delivery": 3,
    "delivered": 4,
    "exception": 2,
    "unknown": 0,
}


def _config():
    """读取 config.json 里的 express_tracking 配置（api_key / base_url）。"""
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = (json.load(f) or {}).get("express_tracking") or {}
    except Exception:
        cfg = {}
    return {
        "api_key": str(cfg.get("api_key") or DEFAULT_API_KEY).strip(),
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "timeout": int(cfg.get("timeout") or DEFAULT_TIMEOUT),
    }


def is_external_waybill(code):
    """非 7 位纯数字 → 视为外部真实快递单号（走 uapis 真实接口）。"""
    return not INTERNAL_WAYBILL_RE.match(str(code or "").strip())


# 本系统 5 档进度的文案（外部单号用这套，保证与进度条一致）
STEP_TEXTS = {0: "待揽收", 1: "已发货", 2: "运输中", 3: "派送中", 4: "已签收"}

# 从轨迹文本推断真实状态：快递公司的 status 字段有时偏保守
# （实测圆通最新一条已是「快件已领取」，接口仍返回 in_transit / is_completed=false），
# 因此以轨迹文本为准做一次校正，只往上取、不回退。
_TRACK_RULES = (
    # 已签收：包含快递柜 / 驿站取出、代收等真实妥投说法
    ("delivered", ("已签收", "已妥投", "已领取", "已取件", "已提取", "已代收",
                   "代收", "签收人", "已投递", "投递成功", "妥投")),
    # 派送中：在派件、已到驿站/快递柜待取
    ("out_for_delivery", ("派送", "派件", "投递中", "待取件", "待自提", "已到达驿站",
                          "菜鸟驿站", "快递柜", "驿站", "自提点", "取件码")),
    # 异常：退回 / 拒收
    ("exception", ("退回", "退件", "拒收", "无人接收", "疑难", "异常", "派送失败")),
    # 运输中
    ("in_transit", ("运输中", "已发出", "已发往", "离开", "到达", "中转", "转运",
                    "分拣", "已抵达", "发往")),
    # 待揽收（必须排在「已揽收」之前：「等待揽收」也含「揽收」）
    ("pending", ("待揽收", "等待揽收", "已下单", "已打印")),
    # 已揽收
    ("picked_up", ("已揽收", "已收寄", "已收件", "已揽件", "揽件", "揽收")),
)


def _track_status(tracks):
    """按时间倒序扫轨迹，取最新一条能识别出状态的轨迹。

    返回 (step, code, time)；识别不出返回 (None, None, "")。
    """
    for t in tracks or []:
        text = str(t.get("context") or "")
        if not text:
            continue
        for code, keys in _TRACK_RULES:
            if any(k in text for k in keys):
                return STATUS_STEP.get(code, 0), code, str(t.get("time") or "")
    return None, None, ""


def _normalize(payload, tracking_number):
    """把 uapis 返回整理成本系统统一的物流结构。"""
    carrier = str(payload.get("carrier_name") or "").strip()
    api_text = str(payload.get("status") or "").strip()
    status_code = str(payload.get("status_code") or "").strip()
    api_step = STATUS_STEP.get(status_code, 0)

    tracks = []
    for t in payload.get("tracks") or []:
        if not isinstance(t, dict):
            continue
        tracks.append({
            "time": str(t.get("time") or ""),
            "context": str(t.get("context") or ""),
        })

    # 用轨迹校正接口状态：只往上取，避免把「已领取」显示成「运输中」
    track_step, _track_code, track_time = _track_status(tracks)
    if track_step is not None and track_step > api_step:
        step = track_step
        status_code = _track_code
        status_text = STEP_TEXTS.get(step, api_text or "未知")
    else:
        step = api_step
        status_text = api_text or STEP_TEXTS.get(step, "未知")

    is_completed = bool(payload.get("is_completed")) or step >= 4
    completed_at = str(payload.get("completed_at") or "")
    # 签收时间：接口没给但轨迹已显示签收/领取时，用那条轨迹的时间
    if step >= 4 and not completed_at and track_time:
        completed_at = track_time

    return {
        "source": "uapi",
        "waybill": str(payload.get("tracking_number") or tracking_number),
        "carrier": carrier or "快递公司",
        "carrier_code": str(payload.get("carrier_code") or ""),
        "status_code": status_code or "unknown",
        "status_text": status_text,
        "status_step": step,
        "is_completed": is_completed,
        "completed_at": completed_at,
        "tracks": tracks,
        "track_count": int(payload.get("track_count") or len(tracks)),
        # 外部单号没有本系统的发货 / 预计送达时间
        "shipped_at": None,
        "estimated_arrival": None,
        "address": "",
    }


def query(tracking_number, phone="", carrier_code="", timeout=None):
    """查询外部真实快递单号。返回 (是否成功, 提示信息, 结果 dict 或 None)。

    tracking_number 必填；phone 为收件人手机尾号 4 位（顺丰等必须）。
    超时默认取 config.json 的 express_tracking.timeout（30 秒），
    因为该接口实测较慢（18~20 秒），超时过短会误报失败。
    """
    code = str(tracking_number or "").strip()
    if not code:
        return False, "请输入快递单号", None

    cfg = _config()
    if not cfg["api_key"]:
        return False, "未配置快递查询接口的 API Key（config.json → express_tracking.api_key）", None

    params = {"tracking_number": code}
    phone = str(phone or "").strip()
    if phone:
        if not re.match(r"^\d{4}$", phone):
            return False, "手机尾号必须是 4 位数字", None
        params["phone"] = phone
    carrier_code = str(carrier_code or "").strip()
    if carrier_code:
        params["carrier_code"] = carrier_code

    if timeout is None:
        timeout = cfg["timeout"]

    try:
        resp = requests.get(
            cfg["base_url"] + QUERY_PATH,
            params=params,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except requests.Timeout:
        return False, "快递查询服务响应超时，请稍后重试", None
    except requests.RequestException as e:
        return False, f"快递查询服务连接失败：{e}", None

    try:
        body = resp.json() or {}
    except ValueError:
        return False, f"快递查询服务返回异常（HTTP {resp.status_code}）", None

    if resp.status_code == 200:
        return True, "查询成功", _normalize(body, code)

    message = str(body.get("message") or "").strip()
    err_code = str(body.get("code") or "").strip()
    if err_code == "NO_TRACKING_DATA" or resp.status_code == 404:
        return False, message or "暂无物流信息，可尝试填上收件人手机尾号后再查", None
    if resp.status_code == 401 or resp.status_code == 403:
        return False, message or "快递查询接口鉴权失败，请检查 API Key", None
    if resp.status_code == 429:
        return False, message or "快递查询接口调用过于频繁，请稍后再试", None
    return False, message or f"快递查询失败（HTTP {resp.status_code}）", None