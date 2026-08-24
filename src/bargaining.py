"""议价内容检测：基于 ASR 全文 + LLM 上下文分析，定位销售/客户之间的价格谈判、
报价、折扣、优惠、砍价、付款金额/方式、合同金额等商务议价对话的起止时间，
返回需整段删除的时间区间。

设计原则（与需求一致）：
- 不凭单个关键词删除一句话，而是结合上下文判定「完整议价对话」。
- 返回整段 [start, end]（绝对时间，秒）并附原因，供人工复核。
- 无法准确判断结束位置时，向两侧适当扩大范围（bargain_pad），但不影响前后
  正常产品演示。
- 无 LLM 时回退到关键词聚类启发式（同样合并相邻句成段，而非单句删除）。

输入 segments 结构（来自 asr.transcribe_with，已叠加绝对 offset）：
  [{"start": float, "end": float, "text": str, ...}, ...]
"""
from __future__ import annotations

import re
from typing import List, Dict, Any

# 议价强信号关键词（命中即高度可能为议价相关句）
STRONG_KEYWORDS = [
    "多少钱", "报价", "价格", "优惠", "打折", "折扣", "便宜", "贵了",
    "打个折", "最低", "底价", "成交价", "到手价", "首付", "分期",
    "付款方式", "支付方式", "合同金额", "合同价", "签合同", "合作价",
    "批发价", "团购价", "活动价", "促销价", "让利", "砍价", "压价",
    "能少", "能优惠", "再便宜", "年费", "月费", "收费", "总价",
    "一套多少钱", "一共多少", "合计", "包年", "包月", "多少钱一年",
    "能降", "降价", "抹零", "赠", "送", "返利", "佣金",
]

# 弱信号（单独出现不认定，仅用于上下文扩展）
WEAK_KEYWORDS = ["元", "万", "块", "钱", "费用", "金额", "这套", "这一套"]

# 与议价无关、用于截断扩展的「明显产品演示」信号
DEMO_KEYWORDS = [
    "功能", "演示", "点击", "这里", "模块", "报表", "库存", "订单",
    "客户", "商品", "菜单", "界面", "系统", "操作", "看见", "比如",
]


def _has(text: str, kws) -> bool:
    return any(kw in text for kw in kws)


def detect(segments: List[Dict[str, Any]], llm, cfg) -> List[Dict[str, Any]]:
    """返回需删除的议价区间列表，元素：
    {"start": float, "end": float, "reason": str, "type": "议价"}
    """
    if not segments:
        return []
    # 优先 LLM 上下文分析
    if llm is not None and llm.available():
        spans = _llm_spans(segments, llm)
        if spans is not None:
            out = []
            for s in spans:
                fin = _finalize(segments, s, cfg)
                if fin:
                    out.append(fin)
            if out:
                return out
    # 回退：关键词聚类启发式
    return _heuristic_spans(segments, cfg)


def _llm_spans(segments: List[Dict[str, Any]], llm) -> Any:
    """调用 LLM 返回 [{start_idx, end_idx, reason}, ...] 或 None。"""
    lines = []
    for i, s in enumerate(segments):
        t0 = _fmt(s["start"])
        t1 = _fmt(s["end"])
        lines.append(f"{i}. [{t0}-{t1}] {s['text']}")
    transcript = "\n".join(lines)
    prompt = (
        "你是视频剪辑审核助手。下面是销售演示视频的字幕（带时间戳，编号从 0 开始）。\n"
        "请识别其中「销售与客户之间的商务议价内容」，包括但不限于：\n"
        "客户询问价格/报价、询问能否优惠/打折、压价砍价；销售介绍具体报价、提出优惠方案；"
        "双方讨论最低价格、合同金额、付款金额或付款方式等。\n\n"
        "要求：\n"
        "1. 必须结合上下文，把一整轮议价对话（从客户开口问价到议价结束）合并成一个连续段，"
        "不要只标单独一句话。\n"
        "2. 返回 JSON 数组，元素形如 {\"start_idx\": 起始句编号, \"end_idx\": 结束句编号, "
        "\"reason\": \"简要说明\" }。编号含两端。\n"
        "3. 若相邻句子属于同一轮谈判，请合并到同一个区间。\n"
        "4. 若无法判断结束位置，end_idx 可适当外延，宁可多删一点。\n"
        "5. 没有议价内容时返回空数组 []。\n"
        "只返回 JSON，不要其他内容。\n\n" + transcript
    )
    try:
        data = llm.chat_json(prompt, temperature=0)
    except Exception as e:  # noqa
        print(f"[warn] LLM 议价检测失败，回退关键词: {e}")
        return None
    if not isinstance(data, list):
        return None
    clean = []
    for d in data:
        if not isinstance(d, dict):
            continue
        si = d.get("start_idx")
        ei = d.get("end_idx")
        if isinstance(si, int) and isinstance(ei, int):
            clean.append({"start_idx": si, "end_idx": ei,
                          "reason": str(d.get("reason", ""))})
    return clean or None


def _finalize(segments: List[Dict[str, Any]], span: Dict[str, Any],
              cfg) -> Dict[str, Any]:
    si = max(0, min(span["start_idx"], len(segments) - 1))
    ei = max(si, min(span["end_idx"], len(segments) - 1))
    start = max(0.0, segments[si]["start"] - cfg.bargain_pad)
    end = segments[ei]["end"] + cfg.bargain_pad
    return {
        "start": float(start),
        "end": float(end),
        "reason": span.get("reason", "商务议价对话"),
        "type": "议价",
    }


def _heuristic_spans(segments: List[Dict[str, Any]],
                     cfg) -> List[Dict[str, Any]]:
    """无 LLM 时的回退：关键词聚类，合并相邻句成段。"""
    flags = [_has(s["text"], STRONG_KEYWORDS) for s in segments]
    out = []
    i = 0
    n = len(segments)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        # 扩展：向前/向后吞并间隔 <= max_gap 且含弱信号的相邻句
        j = i
        while j + 1 < n:
            gap = segments[j + 1]["start"] - segments[j]["end"]
            nxt_has = flags[j + 1] or _has(segments[j + 1]["text"], WEAK_KEYWORDS)
            # 遇到明显产品演示句则停止扩展
            if gap > cfg.bargain_gap:
                break
            if not nxt_has:
                # 允许最多 1 句弱信号缓冲，但再下一句无信号则停
                if j + 2 < n and not (flags[j + 2] or _has(segments[j + 2]["text"], WEAK_KEYWORDS)):
                    break
            if _has(segments[j + 1]["text"], DEMO_KEYWORDS) and not flags[j + 1]:
                # 演示句作为边界，停
                pass
            j += 1
        start = max(0.0, segments[i]["start"] - cfg.bargain_pad)
        end = segments[j]["end"] + cfg.bargain_pad
        hits: set = set()
        for k in range(i, j + 1):
            for kw in STRONG_KEYWORDS:
                if kw in segments[k]["text"]:
                    hits.add(kw)
        reason = "命中议价关键词：" + ",".join(sorted(hits)[:3]) \
            if hits else "商务议价对话"
        out.append({"start": float(start), "end": float(end),
                    "reason": reason, "type": "议价"})
        i = j + 1
    return out


def _fmt(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"
