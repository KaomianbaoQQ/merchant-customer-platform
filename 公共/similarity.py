"""文本相似度工具：用于「忘记密码」时按收货地址做模糊核对。

用户很难把历史地址一字不差地敲出来（省/市/区顺序、门牌号、标点、全角半角
都可能不同），因此不做精确匹配，而是算一个 0~1 的相似度，达到阈值即视为同一地址。

算法：对两段地址做归一化后，取「序列比对比例」与「层级片段集合重叠（Jaccard）」的
加权值。序列比例更看重整体顺序，Jaccard 更看重省/市/区/街道等关键片段是否一致。
"""
import difflib
import re

# 地址相似度达到该阈值即视为命中（0.6 = 较宽松，适合演示 / 毕设场景）
ADDRESS_MATCH_THRESHOLD = 0.6

_HIER = "省市区县镇乡村街道路巷弄里栋幢座单元号楼室层组队"

_PUNCT = re.compile(
    r"[\s，。、（）()\[\]【】""''’'.,/\\_~!@#$%^&*+=|;:?<>．・-]"
)

def normalize(text):
    """归一化：全角转半角、去标点与空白、转小写，便于比对。"""
    if not text:
        return ""
    s = str(text)
    # 全角 ASCII 区（！＂＃…～）转为半角
    s = "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s
    )
    s = _PUNCT.sub("", s).lower()
    return s

def _segments(text):
    """把地址按层级标记切成有意义的片段列表。"""
    s = normalize(text)
    out, buf = [], ""
    for ch in s:
        buf += ch
        if ch in _HIER:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return [x for x in out if x]

def address_similarity(a, b):
    """返回 0~1 的地址相似度。

    · 完全相同 → 1.0；
    · 一方完整包含另一方（如漏写省份）→ 0.9；
    · 否则加权：0.55 × 序列比例 + 0.45 × 片段 Jaccard。
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9

    seq = difflib.SequenceMatcher(None, na, nb).ratio()

    ta, tb = _segments(a), _segments(b)
    if ta and tb:
        inter = set(ta) & set(tb)
        union = set(ta) | set(tb)
        jac = len(inter) / len(union) if union else 0.0
    else:
        jac = 0.0

    return round(0.55 * seq + 0.45 * jac, 3)
