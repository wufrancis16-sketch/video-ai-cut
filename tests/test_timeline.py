"""Step 2 验证：统一剪辑时间轴 EditPlan。

覆盖需求中的关键语义：
- 「十五、字幕必须重新计算时间轴」的原例
- 删除/消音/变速的几何规范化
- 帧栅格对齐（音画同步的前提）
- 待人工确认项不参与删除
- JSON 落盘 / 回读（供人工修改后再渲染）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import timeline as T  # noqa: E402
from src.timeline import EditPlan  # noqa: E402


def approx(a, b, eps=1e-6):
    return abs(a - b) < eps


def test_range_utils():
    assert T.merge_ranges([(0, 2), (1, 3), (5, 6)]) == [(0, 3), (5, 6)]
    assert T.complement([(2, 4)], 10) == [(0, 2), (4, 10)]
    assert T.complement([], 10) == [(0, 10)]
    assert T.subtract([(0, 10)], [(2, 4), (6, 7)]) == [(0, 2), (4, 6), (7, 10)]
    # 完全覆盖 -> 空
    assert T.subtract([(2, 4)], [(0, 10)]) == []
    print("[1] 区间工具 merge/complement/subtract        OK")


def test_subtitle_remap():
    """需求原例：删除 20-25 后，B 字幕应从 25-35 移到 20-30。"""
    plan = EditPlan(duration=35.0, fps=30.0)
    plan.add_delete(20, 25, T.T_NEGOTIATION, "讨论价格")
    plan.subtitle_cues = [
        {"start": 10.0, "end": 20.0, "text": "A"},
        {"start": 25.0, "end": 35.0, "text": "B"},
    ]
    plan.normalize()
    cues = plan.remap_cues()
    assert len(cues) == 2, cues
    a, b = cues
    assert approx(a["start"], 10.0) and approx(a["end"], 20.0), a
    assert approx(b["start"], 20.0) and approx(b["end"], 30.0), b
    assert approx(plan.output_duration(), 30.0), plan.output_duration()
    print("[2] 字幕时间轴重算（需求原例）               OK")


def test_cue_split_across_delete():
    """跨删除边界的字幕拆成两条，不允许出现悬空时间。"""
    plan = EditPlan(duration=30.0, fps=30.0)
    plan.add_delete(10, 15, T.T_HIGH_RISK, "企业微信")
    plan.subtitle_cues = [{"start": 8.0, "end": 18.0, "text": "跨界句"}]
    plan.normalize()
    cues = plan.remap_cues()
    assert len(cues) == 2, cues
    assert approx(cues[0]["start"], 8.0) and approx(cues[0]["end"], 10.0)
    assert approx(cues[1]["start"], 10.0) and approx(cues[1]["end"], 13.0)
    print("[3] 跨删除边界字幕自动拆分                   OK")


def test_mute_kept_not_deleted():
    """敏感业务数据 -> mute，不得进入删除集合。"""
    plan = EditPlan(duration=100.0, fps=25.0)
    plan.add_mute(40.0, 44.0, "客户提到营业额")
    plan.normalize()
    assert plan.delete_ranges() == [], plan.delete_ranges()
    assert approx(plan.output_duration(), 100.0)
    assert plan.mute_ranges() == [(40.0, 44.0)]
    print("[4] 敏感数据只消音不删除                     OK")


def test_mute_reslice_by_delete():
    """消音段与删除段重叠时，重叠部分被切掉（不会指向不存在的画面）。"""
    plan = EditPlan(duration=100.0, fps=25.0)
    plan.add_delete(50, 60, T.T_NEGOTIATION, "议价")
    plan.add_mute(45.0, 55.0, "营业额")
    plan.normalize()
    assert len(plan.mute_segments) == 1
    m = plan.mute_segments[0]
    assert approx(m["start"], 45.0) and approx(m["end"], 50.0), m
    print("[5] 消音段自动避开删除段                     OK")


def test_intro_trim_as_delete():
    plan = EditPlan(duration=200.0, fps=30.0, intro_trim=12.0)
    plan.add_delete(100, 110, T.T_HIGH_RISK, "客户群")
    plan.normalize()
    assert plan.delete_ranges() == [(0.0, 12.0), (100.0, 110.0)]
    assert approx(plan.output_duration(), 200 - 12 - 10)
    # 开场后第一句字幕应落到成片 0 附近
    assert approx(plan.map_time(12.0), 0.0)
    assert plan.map_time(5.0) is None            # 落在删除区
    assert approx(plan.map_time_clamped(5.0), 0.0)
    print("[6] intro_trim 等价删除 + 时间映射           OK")


def test_frame_snap():
    """非整帧边界必须对齐帧栅格，保证音画同步。"""
    fps = 30.0
    plan = EditPlan(duration=10.0, fps=fps)
    plan.add_delete(2.017, 4.049, T.T_LONG_PAUSE, "长停顿")
    plan.normalize()
    keeps = plan.keep_ranges(snap=True)
    for s, e in keeps:
        assert approx(s * fps, round(s * fps), 1e-6), s
        assert approx(e * fps, round(e * fps), 1e-6), e
    total_frames = sum(round((e - s) * fps) for s, e in keeps)
    # 帧数应为整数且与时长一致（无半帧残留）
    assert approx(total_frames / fps, plan.output_duration(), 1e-6)
    print(f"[7] 帧栅格对齐（{total_frames} 帧，无半帧残留） OK")


def test_speed_segments():
    plan = EditPlan(duration=60.0, fps=30.0)
    plan.speed_segments = [{"start": 10.0, "end": 20.0, "speed": 2.0}]
    plan.normalize()
    # 10 秒按 2x -> 5 秒
    assert approx(plan.output_duration(), 55.0), plan.output_duration()
    assert approx(plan.map_time(20.0), 15.0), plan.map_time(20.0)
    print("[8] 变速段时长与映射                         OK")


def test_review_items_not_deleted():
    plan = EditPlan(duration=100.0, fps=30.0)
    plan.add_review(30, 40, T.T_HIGH_RISK, "疑似微信界面但无法确认结束时间")
    plan.normalize()
    assert plan.delete_ranges() == []
    assert approx(plan.output_duration(), 100.0)
    assert "待人工确认" in plan.review_report()
    print("[9] 待人工确认项不自动删除                   OK")


def test_validate_overcut():
    plan = EditPlan(duration=100.0, fps=30.0)
    plan.add_delete(0, 80, T.T_LONG_PAUSE, "过度删除")
    plan.normalize()
    warns = plan.validate()
    assert any("删除比例过高" in w for w in warns), warns
    print("[10] 过度剪辑校验告警                        OK")


def test_save_load_roundtrip():
    tmp = tempfile.mkdtemp(prefix="plan_")
    try:
        plan = EditPlan(source="demo.mp4", duration=120.0, fps=29.97,
                        width=1920, height=1080, intro_trim=5.0)
        plan.add_delete(60, 70, T.T_NEGOTIATION, "报价与优惠")
        plan.add_mute(90, 93, "去年营业额8000万")
        plan.add_review(100, 105, T.T_HIGH_RISK, "疑似交付群")
        plan.subtitle_cues = [{"start": 10, "end": 12, "text": "库存管理"}]
        plan.normalize()
        p = os.path.join(tmp, "plan.json")
        plan.save(p)
        back = EditPlan.load(p)
        assert back.duration == 120.0 and back.fps == 29.97
        assert back.delete_segments[0]["type"] == T.T_NEGOTIATION
        assert back.mute_segments[0]["reason"] == "去年营业额8000万"
        assert len(back.review_items) == 1
        assert approx(back.output_duration(), plan.output_duration())
        print("[11] plan.json 落盘/回读一致                 OK")
        report = plan.review_report()
        assert "议价" in report and "敏感业务数据" in report
        print("[12] 审核清单生成                            OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_range_utils()
    test_subtitle_remap()
    test_cue_split_across_delete()
    test_mute_kept_not_deleted()
    test_mute_reslice_by_delete()
    test_intro_trim_as_delete()
    test_frame_snap()
    test_speed_segments()
    test_review_items_not_deleted()
    test_validate_overcut()
    test_save_load_roundtrip()
    print("\nStep 2 统一剪辑时间轴：全部通过")
