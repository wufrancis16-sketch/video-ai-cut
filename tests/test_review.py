"""Step 6 验证：人工审核决策逻辑（apply_decision / apply_all）。

覆盖：
  A. apply_decision 各决策：delete / keep / 改起止后 delete / skip
  B. apply_all 批量：全部删除 / 全部保留
  C. 决策后 plan 自洽：review_items 减少、delete_segments 正确、normalize 不报错
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.timeline import EditPlan  # noqa: E402
from src import review as R  # noqa: E402


def _plan() -> EditPlan:
    p = EditPlan(source="v.mp4", duration=100.0, fps=30, width=1920, height=1080)
    # 两个待确认项（高风险画面）
    p.add_review(28.0, 31.0, "high_risk_screen",
                 "疑似企业微信界面（边界不确定）")
    p.add_review(60.0, 63.0, "high_risk_screen",
                 "疑似微信聊天记录（置信度不足）")
    p.normalize()
    return p


def test_delete(cfg=None):
    p = _plan()
    d = R.apply_decision(p, 0, "delete")
    assert d == "delete", d
    assert len(p.review_items) == 1, p.review_items
    assert len(p.delete_segments) == 1, p.delete_segments
    ds, de = p.delete_segments[0]["start"], p.delete_segments[0]["end"]
    assert abs(ds - 28.0) < 1e-6 and abs(de - 31.0) < 1e-6, (ds, de)
    print(f"[A1] 确认删除第 1 项 -> 删除段 [{ds:.1f},{de:.1f}]    OK")


def test_keep(cfg=None):
    p = _plan()
    d = R.apply_decision(p, 1, "keep")
    assert d == "keep", d
    assert len(p.review_items) == 1, p.review_items
    assert len(p.delete_segments) == 0, p.delete_segments
    assert abs(p.review_items[0]["start"] - 28.0) < 1e-6, p.review_items
    print(f"[A2] 保留第 2 项 -> 待确认剩 1 项、删除段 0    OK")


def test_edit_bounds(cfg=None):
    p = _plan()
    # 改起止：把 [28,31] 收紧到 [29,30] 后删除
    d = R.apply_decision(p, 0, "delete", new_start=29.0, new_end=30.0)
    assert d == "delete", d
    assert len(p.delete_segments) == 1
    ds, de = p.delete_segments[0]["start"], p.delete_segments[0]["end"]
    assert abs(ds - 29.0) < 1e-6 and abs(de - 30.0) < 1e-6, (ds, de)
    # 越界被裁剪到 [0,100]
    R.apply_decision(p, 0, "delete", new_start=-5.0, new_end=999.0)
    ds, de = p.delete_segments[1]["start"], p.delete_segments[1]["end"]
    assert ds == 0.0 and de == 100.0, (ds, de)
    print(f"[A3] 改起止删除 + 越界裁剪 -> [{ds:.1f},{de:.1f}]    OK")


def test_skip(cfg=None):
    p = _plan()
    d = R.apply_decision(p, 0, "skip")
    assert d == "skip", d
    assert len(p.review_items) == 2, p.review_items  # 不变
    assert len(p.delete_segments) == 0
    print(f"[A4] 跳过 -> 待确认项数量不变（{len(p.review_items)}）   OK")


def test_all_delete(cfg=None):
    p = _plan()
    n = R.apply_all(p, "delete")
    assert n == 2 and len(p.review_items) == 0, (n, p.review_items)
    assert len(p.delete_segments) == 2, p.delete_segments
    print(f"[B1] 全部删除 -> 删除段 {len(p.delete_segments)} 个、"
          f"待确认 0    OK")


def test_all_keep(cfg=None):
    p = _plan()
    n = R.apply_all(p, "keep")
    assert n == 2 and len(p.review_items) == 0, (n, p.review_items)
    assert len(p.delete_segments) == 0
    print(f"[B2] 全部保留 -> 删除段 0、待确认 0    OK")


def test_final_plan_consistent(cfg=None):
    p = _plan()
    # 决策 + normalize + 保存回读，确保 plan 自洽
    R.apply_decision(p, 0, "delete")
    R.apply_decision(p, 0, "keep")  # 原第 2 项现在 index 0
    p.normalize()
    import json
    d = p.to_dict()
    p2 = EditPlan.from_dict(json.loads(json.dumps(d)))
    assert p2.duration == 100.0
    assert len(p2.review_items) == 0
    assert len(p2.delete_segments) == 1
    # keep_ranges 不为空（没有删光）
    assert p2.keep_ranges()
    print(f"[C1] 决策后 plan 落盘回读自洽、保留区间非空    OK")


if __name__ == "__main__":
    test_delete()
    test_keep()
    test_edit_bounds()
    test_skip()
    test_all_delete()
    test_all_keep()
    test_final_plan_consistent()
    print("\nStep 6 人工审核逻辑：全部通过")
