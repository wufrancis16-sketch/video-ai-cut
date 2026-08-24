"""第三阶段：人工审核（仅作用于「待人工确认」项，对应需求「十八、人工审核」）。

审核对象 only 是 plan.review_items（由高风险画面检测等产出的「边界不确定 /
置信不足 / 疑似」片段）。这些片段在 analyze 阶段**绝不自动删除**，必须人工拍板。

交互能力（对应 Step 6 需求）：
  - 列出待确认清单（时间码 + 类型 + 原因 + AI 建议）
  - 逐项确认：删除 / 保留（不删，画面保留）/ 改起止后删除 / 跳过
  - 批量：全部删除 / 全部保留
  - 改完写回 plan.json，render() 下次只读 plan，自动应用决定
  - 同步刷新 审核清单.txt

隐私安全优先：拿不准的片段默认「保留」（skip），不会因误删漏掉隐私，
也不会因默认删除而误伤正常内容。

非交互（CI / 自动化）场景：stdin 非 tty 时 review_plan 不会阻塞，
直接返回当前 plan（相当于全部跳过）。测试通过 apply_decision 直接驱动。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from .timeline import EditPlan, fmt_hms_short, type_label


# ---------------------------------------------------------------------------
# 时间码解析（改起止时用）
# ---------------------------------------------------------------------------
def parse_timecode(s: str) -> float:
    """解析时间码：支持 'MM:SS' / 'HH:MM:SS' / 'HH:MM:SS.mmm' / 纯秒数 '12.5'。"""
    s = (s or "").strip()
    if not s:
        raise ValueError("空时间码")
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        raise ValueError(f"无法解析时间码: {s}")
    return float(s)


# ---------------------------------------------------------------------------
# 核心决策（可单测，无 stdin 依赖）
# ---------------------------------------------------------------------------
def apply_decision(plan: EditPlan, index: int, decision: str,
                   new_start: Optional[float] = None,
                   new_end: Optional[float] = None,
                   reason: Optional[str] = None) -> str:
    """对第 index 个待确认项执行决策。

    decision:
      'delete'  把该片段按当前(或 new_start/new_end 调整后的)边界转为删除段
      'keep'    保留该片段（不删除，画面保留），从待确认清单移除
    其他值视为 'skip'（保持待确认，不改动）。

    返回实际执行的决策字符串。
    """
    if not (0 <= index < len(plan.review_items)):
        return "skip"
    item = plan.review_items[index]
    dur = float(plan.duration or 0.0)

    if decision not in ("delete", "keep"):
        return "skip"  # skip：保持原样

    s = float(new_start if new_start is not None else item["start"])
    e = float(new_end if new_end is not None else item["end"])
    s = max(0.0, min(s, dur))
    e = max(0.0, min(e, dur))
    if e - s <= 1e-3:
        # 边界过短：视为保留，避免误删一帧
        plan.review_items.pop(index)
        return "keep"

    if decision == "delete":
        plan.add_delete(s, e, item.get("type", "high_risk_screen"),
                        reason or item.get("reason", "人工确认删除高风险画面"))
    # keep 不新增删除段
    plan.review_items.pop(index)
    return decision


def apply_all(plan: EditPlan, decision: str) -> int:
    """对全部待确认项执行同一决策。返回实际处理的条目数。"""
    n = len(plan.review_items)
    if decision == "delete":
        for i in range(n - 1, -1, -1):
            apply_decision(plan, i, "delete")
    elif decision == "keep":
        for i in range(n - 1, -1, -1):
            apply_decision(plan, i, "keep")
    return n


# ---------------------------------------------------------------------------
# 交互式审核
# ---------------------------------------------------------------------------
def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def _print_item(idx: int, item: Dict[str, Any]) -> None:
    lab = type_label(item.get("type", ""))
    sug = item.get("suggestion", "delete")
    sug_lab = "建议删除" if sug == "delete" else "建议保留"
    print(f"\n[{idx + 1}] {fmt_hms_short(item['start'])} - "
          f"{fmt_hms_short(item['end'])}  ({item['end'] - item['start']:.1f}s)")
    print(f"    类型：{lab}（{sug_lab}）")
    print(f"    原因：{item.get('reason', '') or '-'}")


def review_plan(plan_path: str, cfg: Any = None) -> EditPlan:
    """加载 plan.json，交互式审核待确认项，写回 plan.json + 审核清单.txt。"""
    plan = EditPlan.load(plan_path)
    plan_path = os.path.abspath(plan_path)

    print("=" * 60)
    print("剪辑方案审核")
    print("=" * 60)
    # 先打印完整清单（含已确定的删除/消音），让审核者看到全貌
    print(plan.review_report())

    if not plan.review_items:
        print("\n✅ 没有待人工确认的项，无需审核。")
        return plan

    interactive = sys.stdin.isatty()
    if not interactive:
        print("\n[info] 非交互环境（stdin 非 tty），跳过人工审核，"
              "保留全部待确认项（不自动删除）。")
        return plan

    print(f"\n共 {len(plan.review_items)} 项待人工确认。")
    print("全局指令： [1]逐项确认(默认)  [A]全部删除  [K]全部保留  "
          "[Q]退出(保存已做的修改)")
    g = _prompt("选择模式 > ").lower()
    if g in ("a", "all"):
        apply_all(plan, "delete")
        print("已标记全部为删除。")
    elif g in ("k", "keep"):
        apply_all(plan, "keep")
        print("已标记全部为保留。")
    elif g in ("q", "quit"):
        pass
    else:
        idx = 0
        while idx < len(plan.review_items):
            item = plan.review_items[idx]
            _print_item(idx, item)
            ans = _prompt(
                "  [d]删除 [k]保留 [e]改起止后删除 [s]跳过(默认) > ").lower()
            if ans in ("d", "delete"):
                apply_decision(plan, idx, "delete")
            elif ans in ("k", "keep"):
                apply_decision(plan, idx, "keep")
            elif ans in ("e", "edit"):
                try:
                    s = parse_timecode(
                        _prompt("    新起点 (HH:MM:SS 或秒) > ") or
                        str(item["start"]))
                    e = parse_timecode(
                        _prompt("    新终点 (HH:MM:SS 或秒) > ") or
                        str(item["end"]))
                except ValueError as ex:
                    print(f"    解析失败：{ex}，本次跳过")
                    idx += 1
                    continue
                apply_decision(plan, idx, "delete", new_start=s, new_end=e)
            else:  # skip
                idx += 1

    # 落盘
    plan.normalize()
    plan.save(plan_path)
    _write_report(plan, plan_path)
    print(f"\n✅ 审核结果已写回：{plan_path}")
    print(f"   剩余待确认项：{len(plan.review_items)}  "
          f"| 删除段：{len(plan.delete_segments)}")
    return plan


def _write_report(plan: EditPlan, plan_path: str) -> None:
    """把最新审核清单写到 plan 同目录的 审核清单.txt。"""
    out = os.path.join(os.path.dirname(plan_path) or ".", "审核清单.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(plan.review_report())
    except Exception:  # noqa
        pass
