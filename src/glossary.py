"""领域术语纠错（后处理）。

背景：faster-whisper tiny 模型在中文 + 商贸行业术语上误识较多
（如「物金」→「五金」、「烧贸行」→「商贸」）。本模块在 ASR 结果之上
做一层保守的短语替换，修正高置信的已知行业错词。

设计原则：
- 只替换「高置信」的短短语，避免误改正确文本。
- 在 sensitive.annotate_segments 之前调用，使脱敏文本也基于纠错后的内容。
- 无论 tiny / small / medium 模型都生效，作为通用后处理层。
"""
from __future__ import annotations

from typing import List, Dict, Any, Tuple

# (误识短语, 正确短语) —— 保守、基于已确认样本的高置信短语
TRADE_TERMS: List[Tuple[str, str]] = [
    # 商贸 / 公司
    ("烧贸行", "商贸"),
    ("烧贸", "商贸"),
    # 五金 / 配件
    ("物金", "五金"),
    ("机械内", "机械类"),
    # 门店 / 零售批发
    ("门电", "门店"),
    ("陷下", "像下"),
    ("缩个", "做"),
    ("材区别", "啥区别"),
    # 口语误识
    ("一下是", "一些是"),
    ("多一下", "多一些"),
    # 对账（用口语化长串，避免误改金融术语"对价"）
    ("对价亚", "对账呀"),
    ("对价呀", "对账呀"),
    # 屏幕共享（2026-08-24 实测：whisper-small 把"共享"高频误识为"共产"，
    # 必须用复合短语替换，绝不能裸替换"共产"→"共享"会误改"共产党"等）
    ("共产党桌面", "共享桌面"),
    ("共产桌面", "共享桌面"),
    ("没有看到共产", "没有看到共享"),
]


def correct_text(text: str) -> str:
    """对单条文本做术语纠错。"""
    if not text:
        return text
    for wrong, right in TRADE_TERMS:
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


def correct_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """就地修正 segments 的 text 字段（脱敏文本会在 annotate 阶段重算）。"""
    for s in segments:
        s["text"] = correct_text(s.get("text", ""))
    return segments
