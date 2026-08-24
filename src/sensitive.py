"""敏感信息检测与脱敏（稳定性优先 · 价格哔声专项，2026-08-21）。

设计原则（用户要求：ERP 正常产品演示里的价格字段绝不消音）：
- 非价格隐私（经营数据：营业额/利润/手机/联系方式/合同金额等）保持「命中即消音」。
- **价格哔声只针对"销售报价沟通"**：必须同时命中报价关键词
  （报价/优惠/最低价/成交价/多少钱/合同金额/一年多少钱…）**且** 同句含
  金额数字，才消音。商品销售价/采购价/成本价/规格/库存等正常产品展示
  价格（即便带数字）一律保留，不消音。
- 议价/还价表达（能不能便宜/打几折…）同上述：含金额数字才消音。
- 总原则：不确定 → KEEP；明确销售报价沟通 → MUTE。绝不把产品演示当报价消音。

对外接口保持兼容：
- find_sensitive_spans(text, context=None) -> [(s, e, matched)]
- annotate_segments(segments)               -> 为每段写入 sensitive_spans/sensitive/redacted_text
- mask_digits / redact_text / add_llm_spans  -> 行为不变
"""
from __future__ import annotations

import re
from typing import List, Dict, Any, Tuple

# ---------------------------------------------------------------------------
# 一、非价格敏感关键词（直接消音，经营数据/隐私，与报价无关）
# ---------------------------------------------------------------------------
NON_PRICE_KW = [
    # 收入 / 利润
    "营业额", "销售额", "营收", "流水", "产值",
    "净利润", "毛利润", "利润率", "毛利率", "净利率", "利润", "毛利", "净利",
    # 经营数据（合同/订单/库存金额、回款、往来账）
    "库存金额", "订单金额", "合同金额", "回款", "应收账款", "应付账款",
    # 数量类
    "客户数量", "订单数量", "库存数量", "客户数", "订单数", "会员数",
    # 主体 / 联系信息
    "客户名称", "联系方式", "供应商信息", "供应商名称",
]

# ---------------------------------------------------------------------------
# 二、销售报价沟通关键词（命中 + 同句含金额数字 → 消音）
# ---------------------------------------------------------------------------
# 用户明确列举：报价 / 优惠 / 最低价 / 成交价 / 多少钱 / 合同金额 / 一年多少钱
QUOTE_KW = [
    "报价", "优惠", "最低价", "成交价", "多少钱", "合同金额", "一年多少钱",
    "一年费用", "服务费", "收费标准", "怎么收费", "收费", "优惠价", "报价方案",
    "商务报价", "实施费用", "软件授权费", "部署费用", "买断",
]

# ---------------------------------------------------------------------------
# 三、议价 / 还价表达（含金额数字才消音，避免把"能不能便宜"这类泛谈消音）
# ---------------------------------------------------------------------------
BARGAIN_KW = [
    "能不能便宜", "能不能优惠", "还能不能低", "还能降多少", "价格还能降吗",
    "最低多少", "给个优惠", "打几折", "价格能谈吗", "价格还能谈",
    "还能谈", "还能降", "还能低", "再便宜一点", "优惠一点", "便宜一点",
    "有没有折扣", "有没有优惠", "可以打几折", "折扣多少", "多少折扣",
    "打几折", "几折", "价格还能不能降", "价格能不能谈", "预算多少",
    "优惠多少", "价格还能再低吗", "再优惠", "能便宜点吗", "能优惠吗",
]

# ---------------------------------------------------------------------------
# 四、产品展示价格（好生意/ERP 正常产品演示，绝不消音）
# ---------------------------------------------------------------------------
PRODUCT_PRICE_KW = [
    "商品销售价", "销售价", "售价", "采购价", "进货价", "成本价", "单价",
    "库存成本", "结算价", "含税价", "不含税价", "商品金额", "零售价",
    "批发价", "会员价", "价格", "价钱", "规格", "库存", "单位", "商品",
]

# ---------------------------------------------------------------------------
# 五、正则（手机号 / 金额）
# ---------------------------------------------------------------------------
# 手机号
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 金额：含量级的数字（8000 / 3.5亿 / 8000万 / 120元 / 报价8000 等）。
# 用于"同句含金额数字"判定，以及报价/议价句中的金额一并消音。
AMOUNT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:亿|千万|百万|万|千|元|块钱|块|人民币)?")

# 衬词：关键词与数值之间的连接词
_FILLER = r"(?:是|有|做|做到|做了|大概|大约|差不多|将近|接近|超过|在|到|" \
          r"个|约|多|了|的|，|,|\s)*"


def _build_kw_value_re(kws):
    alt = "|".join(sorted((re.escape(k) for k in kws), key=len, reverse=True))
    return re.compile(
        rf"(?:{alt}){_FILLER}\d+(?:\.\d+)?\s*"
        rf"(?:亿|千万|百万|万|千|百)?\s*(?:元|块钱|块|人民币|吨|台|件|个|家|单)?")


_NONPRICE_VALUE_RE = _build_kw_value_re(NON_PRICE_KW)
_QUOTE_VALUE_RE = _build_kw_value_re(QUOTE_KW)
_BARGAIN_VALUE_RE = _build_kw_value_re(BARGAIN_KW)

REPLACEMENT = "【哔——】"


# ---------------------------------------------------------------------------
# 上下文辅助
# ---------------------------------------------------------------------------
def _contains_any(ctx: str, kws) -> bool:
    if not ctx:
        return False
    c = ctx.lower()
    return any(kw.lower() in c for kw in kws)


def _near_kw(text: str, s: int, e: int, kws, window: int = 14) -> bool:
    seg = text[max(0, s - window): min(len(text), e + window)]
    return _contains_any(seg, kws)


# ---------------------------------------------------------------------------
# 核心：敏感区间识别
# ---------------------------------------------------------------------------
def find_sensitive_spans(text: str,
                         context: str = None) -> List[Tuple[int, int, str]]:
    """返回 (start, end, matched) 列表，已合并重叠/相邻区间。

    价格哔声规则（稳定性优先，绝不误伤产品演示）：
      1) 非价格隐私（营业额/利润/手机/联系方式…）→ 直接消音。
      2) 销售报价沟通：报价/议价关键词 **且** 同句含金额数字 → 消音
         （关键词本身 + 句中全部金额数字一并消音）。
      3) 金额数字：仅当邻域为 报价/议价/非价格隐私 才消音；
         产品展示价格（商品/规格/库存/单价…）或孤立数字 → 保留。
      4) 产品展示价格（PRODUCT_PRICE_KW 全部词）→ 不参与任何消音。

    text    : 当前 ASR 句
    context : 上下文窗口（默认=text 自身）。
    """
    if not text:
        return []
    raw: List[Tuple[int, int]] = []

    # 1) 非价格隐私：直接消音（经营数据/隐私）
    for kw in NON_PRICE_KW:
        for m in re.finditer(re.escape(kw), text):
            raw.append((m.start(), m.end()))
    for m in _NONPRICE_VALUE_RE.finditer(text):
        raw.append((m.start(), m.end()))
    for m in PHONE_RE.finditer(text):
        raw.append((m.start(), m.end()))

    has_amount = bool(AMOUNT_RE.search(text))
    quote_hit = _contains_any(text, QUOTE_KW + BARGAIN_KW)

    # 2) 销售报价沟通：报价/议价关键词 且 同句含金额数字 → 消音
    if has_amount and quote_hit:
        for kw in QUOTE_KW + BARGAIN_KW:
            for m in re.finditer(re.escape(kw), text):
                raw.append((m.start(), m.end()))
        for m in _QUOTE_VALUE_RE.finditer(text):
            raw.append((m.start(), m.end()))
        for m in _BARGAIN_VALUE_RE.finditer(text):
            raw.append((m.start(), m.end()))

    # 3) 金额数字：仅当邻域为 报价/议价/非价格隐私 才消音；产品/孤立数字保留
    for m in AMOUNT_RE.finditer(text):
        s, e = m.start(), m.end()
        if _near_kw(text, s, e, QUOTE_KW + BARGAIN_KW + NON_PRICE_KW):
            raw.append((s, e))

    # 4) 产品展示价格（PRODUCT_PRICE_KW）：不加入 raw → 绝不消音

    # 合并重叠/相邻
    raw.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s, e, text[s:e]) for s, e in merged]


def _is_mute(text: str, context: str = None) -> bool:
    """便捷判定：该句是否应消音（用于测试/审阅）。"""
    return len(find_sensitive_spans(text, context)) > 0


_DIGIT_RE = re.compile(r"\d")


def mask_digits(s: str) -> str:
    """把数字打掉，用于写入日志/审核清单，避免敏感数值泄露到文本文件。

    例：「营业额8000万」-> 「营业额●●●●万」
    """
    return _DIGIT_RE.sub("●", s or "")


def redact_text(text: str, spans: List[Tuple[int, int, str]]) -> str:
    """从后往前替换，避免下标偏移问题。"""
    out = text
    for s, e, _t in sorted(spans, reverse=True):
        out = out[:s] + REPLACEMENT + out[e:]
    return out


def annotate_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为每个 segment 计算 sensitive_spans / sensitive / redacted_text。

    上下文窗口：取当前句前后各 2 句，用于价格/议价的语境判断。
    """
    n = len(segments)
    for i, seg in enumerate(segments):
        text = seg.get("text", "") or ""
        ctx_parts = []
        for j in range(max(0, i - 2), min(n, i + 3)):
            t = segments[j].get("text", "") or ""
            if t:
                ctx_parts.append(t)
        ctx = " ".join(ctx_parts)
        spans = find_sensitive_spans(text, ctx)
        seg["sensitive_spans"] = spans
        seg["sensitive"] = len(spans) > 0
        seg["redacted_text"] = redact_text(text, spans)
    return segments


def add_llm_spans(segments: List[Dict[str, Any]],
                  llm_hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并 LLM 语义判断命中的短语到对应 segment 的 spans。

    llm_hits 形如: [{"index": 0, "phrase": "8000万"}, ...]
    """
    if not llm_hits:
        return segments

    by_index: Dict[int, List[Tuple[int, int, str]]] = {}
    for hit in llm_hits:
        idx = hit.get("index")
        phrase = (hit.get("phrase") or "").strip()
        if idx is None or not phrase or idx >= len(segments):
            continue
        text = segments[idx]["text"]
        pos = text.find(phrase)
        if pos == -1:
            continue
        by_index.setdefault(idx, []).append((pos, pos + len(phrase), phrase))

    for idx, extra in by_index.items():
        merged = segments[idx]["sensitive_spans"] + extra
        merged.sort(key=lambda x: x[0])
        clean: List[Tuple[int, int, str]] = []
        for s, e, t in merged:
            if clean and s <= clean[-1][1]:
                clean[-1] = (clean[-1][0], max(clean[-1][1], e), clean[-1][2])
            else:
                clean.append((s, e, t))
        segments[idx]["sensitive_spans"] = clean
        segments[idx]["sensitive"] = True
        segments[idx]["redacted_text"] = redact_text(segments[idx]["text"], clean)
    return segments
