"""售后客服 —— Web 端封装。

职责：
    1. 物流问题（没收到 / 丢件等）：先问用户是哪一笔订单，不一次性把所有订单发出去
    2. 退换货 / 退款政策咨询（规则 / 流程 / 时间窗）：直接回政策，不当作提交申请
    3. 退货 / 换货 / 退款：后端确定性处理（Dify 没有退款分支），校验物流并生成新物流单号
    4. 查询订单：直接返回人能看懂的订单文字（不返回 SQL / JSON）
    5. 其它问句：调用「售后客服」Dify 工作流，并把完整上下文上传

「用户指的是哪一笔订单」的命中判定（1 / 3 / 4 都要用）统一交给 Dify 工作流
「订单定位」判断，见 locator.py / locate_order()。

对外主函数：
    handle_after_sales(user, user_input, session_id=None) -> dict
"""
import json
import re

from .workflow import run_dify_workflow
from .locator import locate as dify_locate
from .order import (
    execute_ops, parse_ops_payload, build_user_orders, get_orders_by_user,
    base_goodsname, effective_quantity,
)
from .chat import save_message, get_recent_messages, count_recent_consecutive
from .auth import check_ops_permission
from .returns import (
    create_return, get_returns_by_user, change_active_return_type,
)
from .session_state import (
    set_pending, get_pending, clear_pending,
    mark_human, is_human,
)

# 物流问题：没收到 / 丢件 / 破损 等，需要先确认是哪一单
LOGISTICS_ISSUE_KEYWORDS = (
    "没收到", "未收到", "没有收到", "没收着", "没拿到", "没到货", "没到", "收不到",
    "丢了", "丢失", "丢件", "遗失", "不见了", "少了", "少件", "缺件",
    "寄错", "发错", "错件", "破损", "坏了", "损坏", "没发货", "未发货", "一直没",
)
EXCHANGE_KEYWORDS = ("换货", "换新", "更换", "以旧换新", "换一个", "换一台", "换个")
RETURN_KEYWORDS = ("退货", "退掉", "退单", "不要了", "申请退", "退回")
REFUND_KEYWORDS = ("退款", "退钱", "退费", "只退款")
_RETURN_RE = re.compile(r"退(?!出)\s*(?:货|款|钱|掉|单|回|了|个|[0-9a-zA-Z一-龥])?")
# “换一个 / 帮我换” 这类裸换货诉求
_EXCHANGE_RE = re.compile(r"换(?:货|新|一个|一台|个|台|了)?")
# 查询类关键词（写操作交给 Dify）
QUERY_KEYWORDS = (
    "查", "查询", "看看", "看一下", "我的订单", "订单号", "物流", "快递",
    "进度", "状态", "到哪", "什么时候到", "发货", "订单",
)
WRITE_KEYWORDS = ("删除", "删掉", "新增", "添加", "创建", "录入", "修改", "改成", "改为", "更新")

# 退换货「政策 / 流程 / 时间窗」咨询类问法：只问规则，不是要立刻提交申请
POLICY_KEYWORDS = (
    "政策", "规则", "流程", "条件", "规定", "标准", "说明",
    "怎么退", "如何退", "怎样退", "怎么换", "如何换", "怎样换",
    "怎么申请", "如何申请", "怎么操作", "如何操作",
    "多久", "多长时间", "时间窗口", "几天", "几分钟",
    "能退吗", "可以退吗", "能不能退", "能否退",
    "能换吗", "可以换吗", "能不能换", "能否换",
    "能退款吗", "可以退款吗", "能不能退款",
    "能退", "可以退", "能换", "可以换", "能否退", "能否换",
    "退货吗", "换货吗", "退款吗", "能退换", "可以退换",
    "退到哪里", "退款到哪", "到账",
)
# 明确的「现在就要办理」说法：命中则不按咨询处理，直接走申请流程
APPLY_HINTS = (
    "我要退", "我要换", "我要申请", "申请退", "申请换", "申请退款",
    "帮我退", "帮我换", "给我退", "给我换", "退掉", "换掉", "不要了",
)

RETURN_POLICY_TEXT = (
    "退换货 / 退款政策如下：\n"
    "1. 未发货：退款、退货都无需填写理由，也不用商家同意、不走平台仲裁，"
    "提交后立即完成；未发货不能申请换货。\n"
    "2. 已发货未签收：申请退货 / 换货 / 退款需要填写理由；不受时间窗限制，"
    "退货只能「全部退回」，原运单号不变、只更新售后状态，用户无需寄回。\n"
    "3. 已签收：申请退货 / 换货 / 退款需要填写理由；退货需在签收后 5 分钟内申请，"
    "换货需在签收后 10 分钟内申请，超时将无法申请；可申请部分数量退换，"
    "剩余可退数量内可多次申请。\n"
    "4. 退款不受时间窗限制，签收与否、签收多久都可以申请。\n"
    "5. 商家同意退货 / 换货后，平台会自动生成 7 位寄回快递单号；凭该单号寄回，"
    "并在「我的订单 - 退换货记录」里填写单号、点「我已发出」。同一订单多次换货，每次都是全新单号。\n"
    "6. 换货：商家签收你寄回的商品后，会填写新的 7 位运单号重新发货，"
    "你可以在「我的订单」里继续查看新包裹的物流。\n"
    "7. 商家拒绝时会写明拒绝理由；顾客可据此申请平台仲裁，由平台介入处理。\n"
    "8. 仲裁期间状态显示「平台仲裁中」，在客服这里随时可以查进度。\n"
    "9. 平台仲裁判定支持顾客时，与商家同意完全一致：已签收由平台生成 7 位退货单号，"
    "顾客凭单号寄回；未签收则原运单号不变、整单退回，无需寄回。\n"
    "需要办理的话，直接告诉我「商品名或订单号 + 退/换/退款」，我马上帮你提交申请。"
)

def detect_logistics_issue(text):
    t = (text or "").strip()
    return bool(t) and any(k in t for k in LOGISTICS_ISSUE_KEYWORDS)

def detect_return_intent(text):
    """识别退货 / 换货意图，返回 "return" / "exchange" / None。"""
    t = (text or "").strip()
    if not t:
        return None

    want_exchange = any(k in t for k in EXCHANGE_KEYWORDS) or bool(_EXCHANGE_RE.search(t))
    want_refund = any(k in t for k in REFUND_KEYWORDS)
    want_return = any(k in t for k in RETURN_KEYWORDS) or bool(_RETURN_RE.search(t))

    # 明确要把货退回（退货 / 退换货 / 换货）时，即使提到“退款”也按退货流程
    send_back = ("退货" in t) or ("退换" in t) or want_exchange
    # “退换货”这类统称：默认按退货处理
    if "退换货" in t:
        return "return"
    if want_refund and not send_back:
        return "refund"
    if want_return:
        return "return"
    if want_exchange:
        return "exchange"
    return None

def detect_policy_question(text):
    """是否是退换货 / 售后的「政策 / 流程 / 时间窗」咨询（而非立即提交申请）。

    必须同时满足：
        1. 提到退 / 换 / 退款 / 售后（避免「多久能到」这类物流问题误判）
        2. 命中政策类问法（政策 / 流程 / 怎么退 / 能退吗 / 多久 ...）
        3. 没有「我要退 / 帮我退 / 申请退」这类明确的办理请求
    """
    t = (text or "").strip()
    if not t:
        return False
    if not any(k in t for k in ("退", "换", "售后")):
        return False
    if any(k in t for k in APPLY_HINTS):
        return False
    return any(k in t for k in POLICY_KEYWORDS)

def detect_type_change(text):
    """是否是「把已经申请的售后换个类型」（把换货改成退货 / 我不换要退）。

    只用于判断用户想改主意，具体能不能改由 returns.change_active_return_type 决定。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in ("改成", "改为", "换成", "取消", "不换", "不要换", "不用换", "改一下")):
        return True
    # 「我不换 要退」这类：同一句里既否定换货、又提出退货
    if ("不换" in t or "没换" in t) and any(k in t for k in ("退", "退货", "退掉")):
        return True
    return False

def detect_query_intent(text):
    """是否是查询订单类意图（写操作不在此列）。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in WRITE_KEYWORDS):
        return False
    return any(k in t for k in QUERY_KEYWORDS)

_AFTERSALE_WORDS = ("退换货", "退货", "换货", "退款", "售后")
_STATUS_WORDS = (
    "进度", "怎么样了", "怎样了", "如何了", "到哪", "结果", "状态", "审核",
    "被拒绝", "拒绝理由", "为什么拒绝", "为何拒绝", "通过了吗", "同意了吗",
    "处理得", "多久",
    # 「我的订单退货怎么还没处理好？」这类催办问法
    "还没处理", "没处理", "处理好", "还没好", "怎么还没", "处理完", "处理了",
)

def detect_aftersale_status_query(text):
    """是否在问已有售后单的进度 / 仲裁情况（而不是要新申请一笔）。

    必须排在「退货 / 换货申请」之前判断，否则「我的退货仲裁怎么样了」会因为
    含「退货」二字被当成新申请，反问顾客是哪一笔订单。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 明确说「现在就要办」的话，按申请流程处理
    if any(k in t for k in APPLY_HINTS):
        return False
    # 提到仲裁 / 平台介入：基本都是在问进度
    if "仲裁" in t or "平台介入" in t:
        return True
    return (any(k in t for k in _AFTERSALE_WORDS)
            and any(k in t for k in _STATUS_WORDS))

HUMAN_RE = re.compile(r"(人工|真人|转人工|人工客服|人工服务|真人客服)")
HUMAN_TRIGGER_TIMES = 3   # 连续说出满 3 次「人工」自动转接人工客服
HUMAN_TRANSFER_REPLY = (
    "让你等到现在，真是抱歉。我这就把人工客服接过来，你刚才说的情况这边都有记录，"
    "不用再重复一遍。\n"
    "客服会尽快在这里回复你，你先歇一会儿，有进展我第一时间告诉你。"
)

# 还没了结的售后状态：安抚话术里会拿它说明「我在跟的是哪件事」
_OPEN_RETURN_STATUSES = (
    "requested", "pending", "approved", "shipping", "arbitrating",
    "returning", "delivering", "received",
)

def detect_human_request(text):
    """顾客是否在要求转人工客服。"""
    return bool(HUMAN_RE.search(str(text or "").strip()))

def _latest_open_return(user):
    """最近一条还没了结的售后单；没有返回 None。"""
    try:
        rows = get_returns_by_user(str((user or {}).get("id") or ""))
    except Exception as e:
        print(f"⚠️ 安抚话术读取售后单失败：{e}")
        return None
    for r in rows:
        if str(r.get("status") or "") in _OPEN_RETURN_STATUSES:
            return r
    return None

def _in_transit_order(user):
    """在途订单（已发货、还没签收）；没有返回 None。"""
    try:
        orders = get_orders_by_user(str((user or {}).get("id") or ""))
    except Exception as e:
        print(f"⚠️ 安抚话术读取订单失败：{e}")
        return None
    for o in orders:
        if int(o.get("status_step") or 0) in (1, 2, 3):
            return o
    return None

def _last_real_complaint(session_id):
    """最近一句真正在说问题的顾客消息（跳过单纯喊「人工」的那几句）。"""
    if not session_id:
        return ""
    try:
        msgs = get_recent_messages(session_id, limit=12)
    except Exception:
        return ""
    for m in reversed(msgs):
        if str(m.get("role") or "") != "user":
            continue
        text = str(m.get("content") or "").strip()
        if text and not detect_human_request(text):
            return text
    return ""

def comfort_reply(user, session_id, hits=1):
    """顾客喊「人工」但还没到转接次数：不暴露计数，只顺着上下文安抚。

    上下文来源：最近一句真实抱怨 → 未了结的售后单 → 在途订单 → 待选订单。
    内容只说顾客此刻在意的那件事，不再出现「第几次」「再点几下」这类机械提示。
    """
    issue = _last_real_complaint(session_id)
    pending = get_pending(session_id) or {}
    row = _latest_open_return(user)
    status = str((row or {}).get("status") or "")
    lines = []

    if int(hits or 0) <= 1:
        lines.append("我在的，别着急，你说的这件事我一直跟着。")
    else:
        lines.append("又让你回来问了一次，实在抱歉，你的情况这边都记着。")

    if status == "arbitrating" or "仲裁" in issue or "平台介入" in issue:
        lines.append("平台仲裁已经在处理了，结果一出来我马上告诉你，不用你一遍遍来问。")
    elif status == "rejected" or "拒绝" in issue:
        reason = str((row or {}).get("reject_reason") or "").strip()
        lines.append(
            "商家拒绝的理由我看到了" + (f"（{reason}）" if reason else "")
            + "，这种情况你可以申请平台介入，需要的话我陪你把步骤一步步走完。"
        )
    elif row:
        lines.append(
            f"你这一单{row.get('type_text') or '售后'}（订单 {row.get('order_id')}）现在是"
            f"「{row.get('status_text') or '处理中'}」，我一直帮你盯着，有变化第一时间在这里说。"
        )
    elif detect_logistics_issue(issue) or "物流" in issue or "快递" in issue:
        order = _in_transit_order(user)
        if order:
            eta = str(order.get("estimated_arrival") or "").strip()
            lines.append(
                f"你这一单（订单 {order.get('id')}）还在路上，运单 {order.get('waybill')}"
                + (f"，预计 {eta} 前后到" if eta else "")
                + "。包裹一有动静我就回你，你不用一直守着。"
            )
        else:
            lines.append("包裹的事我记下了，我这就帮你查它现在到哪了，别担心。")
    elif pending.get("orders"):
        lines.append("刚才那几笔订单我先帮你留着，你说哪一笔都行，我直接接着往下办。")
    else:
        lines.append("你的问题我记下了，我先把情况核对清楚，再给你一个明确的答复。")

    lines.append("你先歇一会儿，有结果我立刻回你。")
    return "\n".join(lines)

def answer_human_request(user, user_input, session_id):
    """售后「人工」请求：连续说满 3 次自动转接，未满只按上下文安抚。"""
    if is_human(session_id):
        result = _result("人工客服已经在处理你的问题了，客服会尽快在这里回复你，请稍候。", "human")
        result["needs_human"] = True
        return result

    hits = count_recent_consecutive(session_id, detect_human_request)
    if hits < HUMAN_TRIGGER_TIMES:
        return _result(comfort_reply(user, session_id, hits), "human")

    # 登记到平台「全部对话」：人工客服（平台侧）能看到这条会话并直接回复
    try:
        from 公共 import store
        store.append_message(
            "platform", user["id"], user["name"], "user",
            f"【售后转人工】{user_input}", merchant_name="平台客服",
        )
        store.mark_needs_human("platform", user["id"])
    except Exception as e:
        print(f"⚠️ 转人工登记失败：{e}")

    mark_human(session_id)
    result = _result(HUMAN_TRANSFER_REPLY, "human")
    result["needs_human"] = True
    return result

# 「4」「第4个」「4.」这类纯序号回答
_SELECTION_RE = re.compile(
    r"^\s*(?:第\s*)?([0-9]{1,3})\s*(?:个|号|笔|条|单|项)?\s*[.。、,，)）]?\s*$"
)

def parse_selection(text, total):
    """把「4 / 第4个」解析成 1..total 的序号；不是纯序号或超出范围返回 None。"""
    m = _SELECTION_RE.match(str(text or ""))
    if not m:
        return None
    idx = int(m.group(1))
    return idx if 1 <= idx <= max(0, int(total or 0)) else None

def remember_order_choices(session_id, action, orders, **extra):
    """记住客服刚列出的候选订单，等顾客用序号回答（下一句「4」就能接着办）。"""
    set_pending(session_id, dict({
        "action": action,
        "orders": [str(o.get("id")) for o in (orders or [])],
    }, **extra))

def continue_pending_selection(user, user_input, session_id):
    """顾客用序号回答「是哪一笔订单」时，接着上一轮的动作继续办理。

    没有待选择状态、或顾客这次说的不是序号 → 返回 None，交给后面的分支处理。
    """
    state = get_pending(session_id)
    if not state:
        return None
    idx = parse_selection(user_input, len(state.get("orders") or []))
    if idx is None:
        clear_pending(session_id)   # 顾客改说了别的，这次选择作废
        return None
    clear_pending(session_id)
    order_id = (state.get("orders") or [])[idx - 1]
    action = str(state.get("action") or "")

    if action == "return":
        ok, msg, ret = create_return(
            order_id, user, reason=state.get("reason") or "",
            return_type=state.get("return_type") or "return", quantity=None,
        )
        return _result(msg, "return", ret if ok else None)

    orders = get_orders_by_user(user["id"])
    order = next((o for o in orders if str(o.get("id")) == order_id), None)
    if order is None:
        return _result("这笔订单已经找不到了，请重新告诉我商品名或订单号。", action or "query")
    return _result(format_order_detail(order), action or "query")

def format_aftersale_detail(r):
    """一笔售后记录的详细进度说明（含仲裁 / 拒绝理由 / 退货单号）。"""
    code = str(r.get("status_code") or "")
    rtype = r.get("type_text") or "售后"
    lines = [
        f"你的{rtype}申请（售后单号 {r.get('id')}）当前状态：{r.get('status_text') or '—'}",
        f"商品：{r.get('goodsname') or ''} ×{r.get('quantity') or 1}　原订单：{r.get('order_id')}",
    ]
    if r.get("reject_reason"):
        lines.append(f"商家拒绝理由：{r.get('reject_reason')}")

    if code == "arbitrating":
        lines.append("平台正在介入处理这次纠纷，仲裁期间无需重复提交申请，请耐心等待结果。")
    elif code == "rejected":
        lines.append("你可以在「我的订单 - 退换货记录」里点「申请平台仲裁」，由平台介入处理。")
    elif code in ("approved", "pending"):
        if r.get("return_waybill"):
            lines.append(f"平台已生成退货单号 {r.get('return_waybill')}，"
                         f"请凭该单号寄回，寄出后在「退换货记录」里点「我已发出」。")
        else:
            lines.append("商家已同意，请按页面提示填写快递单号并点「我已发出」。")
    elif code == "shipping":
        lines.append(f"退货运输中（退货单号 {r.get('return_waybill') or '—'}），"
                     f"预计 {r.get('estimated_arrival') or '—'} 完成。")
    elif code in ("done", "refunded"):
        lines.append("这笔售后已经处理完成。")
    elif code == "requested":
        lines.append("还在等商家确认，商家同意或拒绝后会更新状态。")
    return "\n".join(lines)

def summarize_aftersales(rows, asked_arbitration=False):
    """多笔售后记录时只做概览，不罗列全部明细。"""
    total = len(rows)
    groups = {}
    for r in rows:
        key = str(r.get("status_text") or "未知")
        groups[key] = groups.get(key, 0) + 1
    parts = "、".join(f"{name} {cnt} 笔" for name, cnt in groups.items() if cnt)
    lines = [f"你目前共有 {total} 笔退换货 / 退款申请：{parts}。"]
    arbitrating = [r for r in rows if str(r.get("status_code")) == "arbitrating"]
    if arbitrating:
        lines.append("其中这几笔正在平台仲裁中：" +
                     "、".join(f"{r.get('goodsname') or ''}（{r.get('id')}）" for r in arbitrating))
    elif asked_arbitration:
        # 顾客专门问仲裁却没有仲裁中的记录，要明确说清，不能只给个概览
        lines.append("目前没有正在平台仲裁中的申请。")
    lines.append("告诉我商品名或售后单号，我可以帮你看单笔的详细进度。")
    return "\n".join(lines)

def answer_aftersale_status(user, user_input):
    """回答「我的退货仲裁怎么样了」这类售后进度问题。"""
    try:
        rows = get_returns_by_user(user["id"])
    except Exception:
        rows = []
    if not rows:
        return _result("你当前还没有退换货 / 退款申请。"
                       "如果要申请，直接告诉我「商品名或订单号 + 退/换/退款」即可。", "aftersale")

    # 用户点名了某个商品 / 售后单号 → 只答那一笔
    hit = None
    t = str(user_input or "").strip()
    for r in rows:
        rid = str(r.get("id") or "")
        if rid and rid in t:
            hit = r
            break
    if hit is None:
        orders = get_orders_by_user(user["id"])
        order, _ = locate_order(user, t, orders)
        if order is not None:
            same = [r for r in rows if str(r.get("order_id")) == str(order.get("id"))]
            if same:
                hit = same[0]
    if hit is None and len(rows) == 1:
        hit = rows[0]

    if hit is not None:
        return _result(format_aftersale_detail(hit), "aftersale", hit)
    asked_arb = ("仲裁" in t or "平台介入" in t)
    return _result(summarize_aftersales(rows, asked_arbitration=asked_arb), "aftersale")

def _orders_payload(orders):
    """把订单列表整理成「订单定位」工作流可读的 JSON 文本（带显式序号）。"""
    items = [{
        "序号": i,
        "订单号": o.get("id"),
        "商品": o.get("goodsname"),
        "运单号": o.get("waybill"),
        "物流状态": o.get("status_text"),
    } for i, o in enumerate(orders, 1)]
    return json.dumps(items, ensure_ascii=False) + "（这是用户可能的订单，已按顺序列出）"

# 中英文俗称 → 英文名/同义词（如「苹果电脑」匹配 MacBook）
PRODUCT_ALIASES = {
    "苹果": ["apple", "mac", "macbook", "iphone", "ipad"],
    "电脑": ["computer", "pc", "laptop", "notebook", "macbook"],
    "笔记本": ["laptop", "notebook", "macbook", "computer"],
    "平板": ["ipad", "tablet"],
    "手机": ["iphone", "phone", "mobile"],
    "鼠标": ["mouse"],
    "键盘": ["keyboard"],
    "耳机": ["earphone", "headphone", "airpods", "earbuds"],
    "显示器": ["monitor", "display", "screen"],
    "鸟": ["bird", "dove", "pigeon"],
    "斑鸠": ["dove", "pigeon", "turtledove"],
}

def _name_features(s):
    """把名称拆成英文 token + 中文字 / 相邻字二元组，用于模糊匹配。"""
    s = str(s or "").lower()
    latin = {t for t in re.findall(r"[a-z0-9]{2,}", s)}
    chars, bigrams = set(), set()
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        chars.update(run)
        bigrams.update(run[i:i + 2] for i in range(len(run) - 1))
    return {"latin": latin, "chars": chars, "bigrams": bigrams}

def _query_latin_terms(text):
    """查询里的英文 token + 中文俗称展开的英文词。"""
    t = str(text or "").lower()
    terms = {x for x in re.findall(r"[a-z0-9]{2,}", t)}
    for zh, ens in PRODUCT_ALIASES.items():
        if zh in t:
            terms.update(ens)
    return terms

def _match_score(text, name):
    """返回 (是否实质命中, 得分)。英文 token 精确包含权重最高。"""
    qf = _name_features(text)
    nf = _name_features(name)
    q_latin = _query_latin_terms(text)

    latin_hit = set()
    for tok in nf["latin"]:
        if len(tok) < 3:
            continue
        if tok in q_latin or any(len(q) >= 3 and (q in tok or tok in q) for q in q_latin):
            latin_hit.add(tok)

    common_chars = nf["chars"] & qf["chars"]
    common_bg = nf["bigrams"] & qf["bigrams"]

    # 查询侧中文覆盖率（如「显示器」三字都在「办公电脑显示器」中）
    q_chars_n = len(qf["chars"])
    q_cover = (len(common_chars) / q_chars_n) if q_chars_n else 0.0
    # 名称侧覆盖率
    name_units = len(nf["latin"]) + max(1, len(nf["bigrams"]))
    n_cover = (len(latin_hit) + len(common_bg)) / name_units

    substantial = bool(latin_hit) or len(common_chars) >= 2 or len(common_bg) >= 1
    # 命中判定：英文 token 精确匹配、或中英文共享词（二元组）命中即可；
    # 否则再看覆盖率，避免「键盘也要退货」这类带上下文后缀的句子匹配不上商品。
    hit = substantial and (bool(latin_hit) or bool(common_bg)
                           or q_cover >= 0.6 or n_cover >= 0.5)
    score = len(latin_hit) * 10 + len(common_bg) * 2 + len(common_chars)
    return hit, score

def best_product_match(orders, text):
    """挑出与用户输入最匹配的订单（支持中英文俗称，如「苹果电脑」→ MacBook）。"""
    best, best_score = None, -1
    for o in orders:
        hit, score = _match_score(text, o.get("goodsname"))
        if hit and score > best_score:
            best, best_score = o, score
    return best

def find_order_in_text(orders, text):
    """
    在用户订单里定位：
        1. 商品名（模糊，允许“商务笔记本电脑”匹配“商务办公笔记本电脑”）
        2. 订单号
        3. 运单号
    找不到返回 None。
    """
    t = text or ""
    order = best_product_match(orders, t)
    if order is not None:
        return order
    for o in orders:
        if o.get("id") and str(o["id"]) in t:
            return o
    for o in orders:
        if o.get("waybill") and str(o["waybill"]) in t:
            return o
    return None

def locate_order(user, user_input, orders=None):
    """
    定位用户消息指的是哪一笔订单 —— 命中判定交给 Dify 工作流「订单定位」做
    （本地不用英文 token / 覆盖率阈值等硬编码规则判定命中）。

    返回 (订单 dict 或 None, 数量或 None)。
    流程：
        1. 调 Dify「订单定位」，取 order_id / quantity
        2. order_id 命中该用户自己的订单 → 用它（数量由用户原话优先，Dify 的作补充）
        3. Dify 不可用 / 判定为空 / order_id 不属于该用户 → 回退 find_order_in_text 兜底
    """
    if orders is None:
        orders = get_orders_by_user(user["id"])
    if not orders:
        return None, None

    pick = dify_locate(user_input, build_user_orders(user)) or {}
    order_id = str(pick.get("order_id") or "").strip() or None
    qty = pick.get("quantity")
    if not (isinstance(qty, int) and qty > 0):
        qty = None

    if order_id:
        for o in orders:
            if str(o.get("id")) == order_id:
                return o, qty

    return find_order_in_text(orders, user_input), qty

def resolve_return_order(user, user_input, orders=None):
    """
    定位用户要退 / 换的那一笔订单。

    返回 (订单 dict 或 None, 订单列表, 数量或 None)。
    流程：
        1. 命中判定由 Dify「订单定位」给出（含商品名语义匹配、订单号、运单号）
        2. 判不出来时：只有一笔订单 → 就是它（不存在歧义）
        3. 有多笔订单又判不出来 → 返回 None，由调用方请顾客提供订单号 / 商品名，
           不替顾客猜（避免退错单）
    """
    if orders is None:
        orders = get_orders_by_user(user["id"])
    if not orders:
        return None, [], None

    order, qty = locate_order(user, user_input, orders)
    if order is not None:
        return order, orders, qty
    if len(orders) == 1:
        return orders[0], orders, qty
    return None, orders, qty

def ask_which_order(orders, action="退货 / 换货"):
    """顾客说不清是哪一笔订单时，请他提供序号 / 订单号 / 商品名（附简短候选）。"""
    lines = [f"请告诉我你要{action}的是哪一笔订单呢？回复序号、订单号或商品名即可："]
    for i, o in enumerate(orders, 1):
        name = base_goodsname(o.get("goodsname")) or "(无商品)"
        qty = int(o.get("quantity") or effective_quantity(o) or 1)
        lines.append(f"{i}. {name} ×{qty}（订单号 {o.get('id')}）")
    return "\n".join(lines)

def format_order_line(index, o):
    """一行订单摘要（人话）。"""
    name = base_goodsname(o.get("goodsname")) or "(无商品)"
    qty = int(o.get("quantity") or effective_quantity(o) or 1)
    name_qty = f"{name} ×{qty}" if qty > 1 else name
    parts = [f"{index}. {name_qty}",
             f"订单号 {o.get('id')}",
             f"下单 {o.get('create_time') or '—'}",
             f"物流：{o.get('status_text') or '—'}"]
    if o.get("waybill"):
        parts.append(f"运单号 {o['waybill']}")
    return "｜".join(parts)

def format_order_detail(o):
    """单笔订单详情（人话）。"""
    name = base_goodsname(o.get("goodsname")) or "(无商品)"
    qty = int(o.get("quantity") or effective_quantity(o) or 1)
    lines = [
        f"订单号：{o.get('id')}",
        f"商品：{name} ×{qty}",
        f"下单时间：{o.get('create_time') or '—'}",
    ]
    if o.get("returnable_quantity") is not None:
        lines.append(f"可退换数量：{o.get('returnable_quantity')} 件")
    if o.get("waybill"):
        lines.append(f"运单号：{o['waybill']}")
    lines.append(f"物流状态：{o.get('status_text') or '—'}")
    if o.get("shipped_at"):
        lines.append(f"发货时间：{o['shipped_at']}")
    if o.get("estimated_arrival"):
        lines.append(f"预计送达：{o['estimated_arrival']}")
    if o.get("address"):
        lines.append(f"收货地址：{o['address']}")
    return "\n".join(lines)

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_QTY_RE = re.compile(r"([0-9]+|一|两|二|三|四|五|六|七|八|九|十)\s*(?:个|件|台|把|条|只|支|张|块|瓶|盒|包|套)")

def parse_quantity(text):
    """从“退两个 / 换 3 件”里解析数量；解析不到返回 None。"""
    m = _QTY_RE.search(text or "")
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)

def _result(reply, branch, return_row=None):
    return {"ok": True, "reply": reply, "return": return_row, "branch": branch, "error": None}

def handle_logistics_issue(user, user_input, session_id=None):
    orders = get_orders_by_user(user["id"])
    if not orders:
        return _result("你当前还没有订单，请核对一下下单账号哦。", "logistics")

    order, _ = locate_order(user, user_input, orders)
    if order:
        return _result(format_order_detail(order), "logistics")

    # 没指明具体订单 → 只问是哪一笔，附上简短候选（不展开全部详情），
    # 并记住候选列表：顾客下一句直接回「4」也能接上
    remember_order_choices(session_id, "logistics", orders)
    lines = ["好的，请告诉我具体是哪一笔订单呢？回复序号或订单号即可："]
    for i, o in enumerate(orders, 1):
        name = base_goodsname(o.get("goodsname")) or "(无商品)"
        lines.append(f"{i}. {name}（{o.get('id')}）")
    return _result("\n".join(lines), "logistics")

def summarize_orders(orders):
    """订单概览总结：只说数量与状态分布，不罗列每笔订单的详细信息。"""
    total = len(orders)
    groups = {}
    for o in orders:
        key = str(o.get("status_text") or "未知")
        groups[key] = groups.get(key, 0) + 1
    parts = "、".join(f"{name} {cnt} 笔" for name, cnt in groups.items() if cnt)
    latest = base_goodsname(orders[0].get("goodsname")) if orders else ""
    lines = [f"你目前共有 {total} 笔订单：{parts}。"]
    if latest:
        lines.append(f"最近一笔是「{latest}」（{orders[0].get('status_text') or '—'}）。")
    lines.append("回复序号、商品名或订单号，我可以帮你查单笔订单的详细物流。")
    return "\n".join(lines)

def handle_query_orders(user, user_input, session_id=None):
    orders = get_orders_by_user(user["id"])
    if not orders:
        return _result("你当前还没有订单。", "query")

    order, _ = locate_order(user, user_input, orders)
    if order:
        return _result(format_order_detail(order), "query")

    # 没有定位到具体订单 → 只做总结，不把所有订单明细列一遍；
    # 记住候选列表，顾客下一句回序号也能直接看那一笔
    remember_order_choices(session_id, "query", orders)
    return _result(summarize_orders(orders), "query")

def format_order_query_result(rows, user_input=None):
    """把 cs.order 查询结果转成人话（用于 Dify select 兜底）。

    用户输入里带具体商品名时，只返回该商品的订单详情（命中判定同样交给 Dify「订单定位」）；
    订单较多且没有明确指向时，给状态总结而不是罗列全部明细。
    """
    if not rows:
        return "没有查询到符合条件的订单。"
    if user_input:
        pick = dify_locate(user_input, _orders_payload(rows)) or {}
        order_id = str(pick.get("order_id") or "").strip()
        match = next((r for r in rows if str(r.get("id")) == order_id), None) if order_id else None
        if match is not None:
            return format_order_detail(match)
    if len(rows) > 3:
        return summarize_orders(rows)
    lines = [f"为你查询到 {len(rows)} 笔订单："]
    for i, r in enumerate(rows, 1):
        lines.append(format_order_line(i, r))
    return "\n".join(lines)

def handle_return_request(user, user_input, return_type, session_id=None):
    order, orders, pick_qty = resolve_return_order(user, user_input)
    if not orders:
        return _result("你当前还没有订单，暂时无法申请退货/换货。", "return")
    if order is None:
        # 说不清是哪一笔订单 → 请顾客提供订单号 / 商品名，不替他猜；
        # 同时记住候选列表，顾客直接回「4」就能接着提交申请
        remember_order_choices(session_id, "return", orders,
                               return_type=return_type, reason=user_input)
        return _result(ask_which_order(orders), "return")

    if return_type in ("return", "exchange") and detect_type_change(user_input):
        changed, changed_msg, changed_ret = change_active_return_type(
            order["id"], return_type, user_id=user["id"], reason=user_input
        )
        if changed:
            return _result(changed_msg, "return", changed_ret)

    qty = parse_quantity(user_input) or pick_qty
    ok, msg, ret = create_return(
        order["id"], user, reason=user_input, return_type=return_type, quantity=qty
    )
    return _result(msg, "return", ret if ok else None)

def answer_policy_question(user, user_input, session_id=None):
    """退换货 / 退款政策咨询：走 Dify 知识库分支作答；Dify 不可用时回退本地权威政策。"""
    dify = ask_after_sales_workflow(user, user_input, session_id)
    if dify.get("ok") and str(dify.get("policy") or "").strip():
        return str(dify["policy"]).strip()
    return RETURN_POLICY_TEXT

def build_dify_context(user, session_id=None):
    """订单 + 退换货记录 + 最近对话历史，作为 user_orders 上传，帮助 Dify 理解意图。"""
    parts = [build_user_orders(user)]

    try:
        returns = get_returns_by_user(user["id"])
    except Exception:
        returns = []
    if returns:
        slim = [{
            "售后单号": r.get("id"),
            "类型": r.get("type_text"),
            "原订单": r.get("order_id"),
            "商品": r.get("goodsname"),
            "数量": r.get("quantity"),
            "状态": r.get("status_text"),
            "退货单号": r.get("return_waybill") or "",
            "申请理由": r.get("reason") or "",
            "商家拒绝理由": r.get("reject_reason") or "",
            "申请时间": r.get("create_time") or "",
        } for r in returns]
        parts.append("【该用户退换货记录】\n" + json.dumps(slim, ensure_ascii=False))

    if session_id:
        msgs = get_recent_messages(session_id, limit=10)
        if msgs:
            lines = [f"{m.get('role')}: {m.get('content')}" for m in msgs if m.get("content")]
            if lines:
                parts.append("【最近对话历史】\n" + "\n".join(lines))

    return "\n\n".join(parts)

def ask_after_sales_workflow(user, user_input, session_id=None):
    return run_dify_workflow(
        user_input=user_input,
        user_id=user["id"],
        user_role=user["role"],
        user_name=user["name"],
        user_orders=build_dify_context(user, session_id),
    )

def _format_dify_reply(dify, user, user_input=None):
    reply_lines = []

    ops_list, err_text = parse_ops_payload(dify["biz_raw"])
    if err_text is not None:
        text = err_text if isinstance(err_text, str) else json.dumps(err_text, ensure_ascii=False)
        reply_lines.append(text)
    elif ops_list:
        allowed, denied = check_ops_permission(ops_list, user)
        for msg in denied:
            reply_lines.append(f"⛔ {msg}")
        if allowed:
            results = execute_ops(
                allowed,
                user_name=user["name"],
                user_id=user["id"],
                user_role=user["role"],
            )
            for res in results:
                if res["operate"] == "select" and res["success"]:
                    reply_lines.append(
                        format_order_query_result(res.get("data") or [], user_input)
                    )
                else:
                    flag = "✅" if res["success"] else "❌"
                    reply_lines.append(f"{flag} [{res['operate']}] {res['message']}")

    if dify.get("talk") is not None:
        reply_lines.append(str(dify["talk"]))
    if dify.get("policy") is not None:
        reply_lines.append(str(dify["policy"]))
    if dify.get("aftersale") is not None:
        reply_lines.append(str(dify["aftersale"]))
    if dify.get("comfort") is not None:
        reply_lines.append(str(dify["comfort"]))
    if dify.get("node_error_message") or dify.get("node_error_type"):
        reply_lines.append(f"[节点错误] {dify.get('node_error_message') or dify.get('node_error_type')}")

    return reply_lines

def handle_after_sales(user, user_input, session_id=None):
    """
    user: {"name","id","role","is_admin",...}
    返回 dict：ok / reply / return / branch / error（转人工后额外带 needs_human=True）
    分支顺序：
        顾客用序号回答是哪一笔订单 → 转人工客服 → 物流问题（先问是哪一单）
        → 退换货政策咨询 → 售后单进度/仲裁查询 → 退货/换货/退款申请
        → 查询订单（人话）→ Dify
    """
    # 0) 顾客用序号（「4」）回答上一轮的「是哪一笔订单」：接着上一轮的动作办理
    result = continue_pending_selection(user, user_input, session_id)
    if result is not None:
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 1) 转人工客服：连续说满 3 次「人工」自动转接（右下角也有「人工」按钮）
    if detect_human_request(user_input):
        result = answer_human_request(user, user_input, session_id)
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 2) 物流问题（没收到 / 丢件）：先确认是哪一笔订单
    if detect_logistics_issue(user_input):
        result = handle_logistics_issue(user, user_input, session_id)
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    if detect_policy_question(user_input):
        reply = answer_policy_question(user, user_input, session_id)
        result = _result(reply, "policy")
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 4) 已有售后单的进度 / 仲裁查询：必须排在申请流程之前，
    #    否则「我的退货仲裁怎么样了」会因为含「退货」被当成新申请
    if detect_aftersale_status_query(user_input):
        result = answer_aftersale_status(user, user_input)
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 5) 退货 / 换货 / 退款申请
    intent = detect_return_intent(user_input)
    if intent:
        result = handle_return_request(user, user_input, intent, session_id)
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 6) 查询订单：直接返回人话
    if detect_query_intent(user_input):
        result = handle_query_orders(user, user_input, session_id)
        if session_id:
            save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
        return _mark_human_channel(result, session_id)

    # 7) 其它：走 Dify
    result = {"ok": False, "reply": "", "return": None, "branch": None, "error": None}
    dify = ask_after_sales_workflow(user, user_input, session_id)

    if not dify["ok"]:
        result["error"] = dify.get("error_message") or "售后工作流调用失败"
        result["reply"] = "抱歉，售后客服暂时无法连接，请稍后再试。"
    elif str(dify.get("return_pick") or "").strip():
        # 售后工作流的分类器判定为「退换货订单定位」→ 按退换货申请办理
        # （正则没识别到的退换货说法，由这里的 Dify 分支兜住）
        result = handle_return_request(
            user, user_input, detect_return_intent(user_input) or "return", session_id
        )
    else:
        reply_lines = _format_dify_reply(dify, user, user_input)
        if not reply_lines:
            reply_lines.append("抱歉，我暂时没有理解你的问题，可以换种说法再试一次吗？")
        result["ok"] = True
        result["reply"] = "\n".join(x for x in reply_lines if x)
        result["branch"] = "dify"

    if session_id:
        save_message(session_id, user["id"], user["name"], "assistant", result["reply"])
    return _mark_human_channel(result, session_id)

def _mark_human_channel(result, session_id):
    """已在人工通道的会话，每次回复都带上 needs_human，前端才会持续收取人工客服消息。"""
    if session_id and is_human(session_id):
        result["needs_human"] = True
    return result
