"""Step 3 验证：分析阶段（纯文本/时间轴逻辑，不碰视频编码）。

用一段人工构造的「销售演示」ASR 结果覆盖全部规则：
  正常演示 / 长停顿 / 1~3s 停顿 / 议价对话 / 客户提问 /
  敏感业务数据（营业额）/ 纯口头禅 / 企业微信关键词
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import analyze as A  # noqa: E402
from src import glossary, sensitive, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.timeline import EditPlan  # noqa: E402


def W(text, start, end):
    """构造带词级时间轴的句子（每字均分，够用于测试映射）。"""
    n = max(1, len(text))
    step = (end - start) / n
    words = [{"word": ch, "start": start + i * step,
              "end": start + (i + 1) * step} for i, ch in enumerate(text)]
    return {"start": start, "end": end, "text": text, "words": words}


def build_segments():
    return [
        W("今天给大家演示一下库存管理功能", 1.0, 5.0),
        W("我们先看采购入库这个模块", 5.4, 9.0),
        # 长停顿 9.0 -> 16.0 (7s)
        W("点击这里可以看到库存台账", 16.0, 20.0),
        W("嗯", 20.2, 20.6),                          # 纯口头禅
        W("这个", 20.8, 21.2),                        # 纯口头禅
        W("那个然后", 21.4, 22.0),                    # 纯口头禅组合
        # 1~3s 停顿 22.0 -> 24.2 (2.2s)
        W("报表中心可以导出月度汇总", 24.2, 28.0),
        # 客户提问
        W("老师我想问一下这个库存预警能不能设置", 30.0, 35.0),
        W("可以的在系统设置里面配置阈值", 35.4, 39.0),
        W("我们公司有三个仓库怎么办", 39.4, 43.0),
        W("多仓库是支持的每个仓库独立核算", 43.4, 47.0),
        # 敏感业务数据
        W("我们去年营业额8000万主要做华东市场", 48.0, 53.0),
        # 议价对话
        W("你们这个系统多少钱", 55.0, 57.5),
        W("标准版报价是三万八", 57.8, 60.5),
        W("还能优惠吗", 60.8, 62.5),
        W("如果确定合作可以给您优惠", 62.8, 66.0),
        W("最低多少钱能签", 66.2, 68.5),
        # 恢复演示
        W("我们继续看一下出库单的操作界面", 72.0, 76.0),
        # 高风险画面关键词
        W("我给您看一下企业微信里的交付群", 80.0, 84.0),
        W("回到系统我们看下财务对账功能", 95.0, 99.0),
    ]


def prep(cfg):
    segs = build_segments()
    segs = glossary.correct_segments(segs)
    segs = sensitive.annotate_segments(segs)
    A._mark_customer_turns(segs)
    return segs


def test_customer_turn_marking(cfg, segs):
    q = [s for s in segs if s.get("is_question")]
    assert any("库存预警能不能设置" in s["text"] for s in q), \
        [s["text"] for s in q]
    assert any("三个仓库怎么办" in s["text"] for s in q)
    prot = [s["text"] for s in segs if s.get("protected")]
    # 客户提问后的销售回答也应被保护
    assert any("系统设置里面配置阈值" in t for t in prot), prot
    print(f"[1] 客户提问识别 {len(q)} 句 / 受保护 {len(prot)} 句       OK")


def test_mute_from_sensitive(cfg, segs):
    mutes = A._build_mute_segments(segs, cfg)
    assert mutes, "应识别出营业额敏感数据"
    m = mutes[0]
    # 应精确落在「营业额8000万」附近，而不是整句 48~53
    assert 48.0 <= m["start"] <= 52.0, m
    assert m["end"] <= 53.5, m
    assert m["end"] - m["start"] < 4.0, f"消音范围过大: {m}"
    assert "企业经营数据" in m["reason"]
    print(f"[2] 敏感数据局部消音 {m['start']:.2f}-{m['end']:.2f}s        OK")


def test_subtitle_redaction(cfg, segs):
    cues = A._build_cues(segs)
    joined = " ".join(c["text"] for c in cues)
    assert "8000万" not in joined, "字幕仍暴露敏感金额！"
    assert sensitive.REPLACEMENT in joined
    print("[3] 字幕已脱敏（无 8000万）                    OK")


def test_pause_plan(cfg, segs):
    deletes, speeds = A._build_pause_plan(segs, 100.0, cfg)
    assert speeds == [], "trim 模式不应产生变速段"
    long_cut = [d for d in deletes if "长停顿" in d["reason"]]
    assert long_cut, deletes
    # 9.0~16.0 的 7s 停顿应被删除，仅保留 pause_delete_keep
    lc = [d for d in long_cut if 8.5 < d["start"] < 10.0][0]
    kept = 7.0 - (lc["end"] - lc["start"])
    assert abs(kept - cfg.pause_delete_keep) < 0.05, kept
    # 22.0~24.2 的 2.2s 停顿应裁短到 pause_trim_to
    short_cut = [d for d in deletes if 22.0 <= d["start"] < 23.0]
    assert short_cut, deletes
    kept2 = 2.2 - (short_cut[0]["end"] - short_cut[0]["start"])
    assert abs(kept2 - cfg.pause_trim_to) < 0.05, kept2
    print(f"[4] 停顿处理：长停顿留 {kept:.2f}s / 短停顿留 {kept2:.2f}s  OK")


def test_pause_protect_qa(segs):
    """客户问答区间内的停顿保留更多，避免剪碎问题。"""
    cfg_p = Config(protect_customer_qa=True, customer_pause_keep=0.6)
    cfg_n = Config(protect_customer_qa=False)
    d_p, _ = A._build_pause_plan(segs, 100.0, cfg_p)
    d_n, _ = A._build_pause_plan(segs, 100.0, cfg_n)
    cut_p = sum(d["end"] - d["start"] for d in d_p)
    cut_n = sum(d["end"] - d["start"] for d in d_n)
    assert cut_p < cut_n, (cut_p, cut_n)
    print(f"[5] 客户问答保护：删除 {cut_p:.2f}s < 无保护 {cut_n:.2f}s   OK")


def test_filler(cfg, segs):
    fillers = A._filler_segments(segs)
    texts = [f["reason"] for f in fillers]
    assert len(fillers) >= 3, texts
    assert all(f["end"] - f["start"] < 1.0 for f in fillers)
    # 不能误删正常句子
    joined = " ".join(texts)
    assert "库存" not in joined and "报表" not in joined, joined
    print(f"[6] 口头禅删除 {len(fillers)} 句，未误删正常句       OK")


def test_speed_mode(segs):
    cfg_s = Config(pause_mode="speed", pause_speed_factor=1.5)
    deletes, speeds = A._build_pause_plan(segs, 100.0, cfg_s)
    assert speeds, "speed 模式应产生变速段"
    assert all(abs(s["speed"] - 1.5) < 1e-6 for s in speeds)
    # >3s 停顿仍然是删除
    assert any("长停顿" in d["reason"] for d in deletes)
    print(f"[7] speed 模式：{len(speeds)} 段变速 + 长停顿仍删除   OK")


def test_full_plan_assembly(cfg, segs):
    """把各部分装进 EditPlan，检查整体自洽性。"""
    plan = EditPlan(source="demo.mp4", duration=100.0, fps=30.0,
                    width=1920, height=1080)
    # 议价（模拟 bargaining 输出）
    plan.add_delete(54.5, 69.0, T.T_NEGOTIATION, "销售与客户讨论价格及优惠")
    # 高风险画面（模拟 risk_screen 输出）
    plan.add_delete(79.5, 90.0, T.T_HIGH_RISK, "企业微信/交付群界面")
    for m in A._build_mute_segments(segs, cfg):
        plan.add_mute(m["start"], m["end"], m["reason"])
    d, sp = A._build_pause_plan(segs, 100.0, cfg)
    for x in d:
        plan.add_delete(x["start"], x["end"], x["type"], x["reason"])
    plan.speed_segments.extend(sp)
    for f in A._filler_segments(segs):
        plan.add_delete(f["start"], f["end"], T.T_FILLER, f["reason"])
    plan.subtitle_cues = A._build_cues(segs)
    plan.normalize()

    # 议价内容必须被完整删除
    for t in (55.5, 60.0, 66.5):
        assert plan.map_time(t) is None, f"议价时间点 {t} 未删除"
    # 高风险画面必须被完整删除
    for t in (80.5, 85.0, 89.0):
        assert plan.map_time(t) is None, f"高风险时间点 {t} 未删除"
    # 客户提问必须保留
    for t in (31.0, 34.0, 40.0, 45.0):
        assert plan.map_time(t) is not None, f"客户提问 {t} 被误删"
    # 敏感数据画面保留、仅消音
    assert plan.map_time(50.0) is not None, "敏感数据段画面不应删除"
    assert plan.mute_ranges(), "应存在消音段"
    # 字幕不含敏感数据
    assert "8000万" not in " ".join(c["text"] for c in plan.subtitle_cues)
    # 字幕重映射后不越界
    remapped = plan.remap_cues()
    out_dur = plan.output_duration()
    assert all(c["end"] <= out_dur + 1e-6 for c in remapped), "字幕超出成片时长"
    assert all(c["start"] >= -1e-9 for c in remapped)
    # 议价/高风险的字幕不应出现在成片
    joined = " ".join(c["text"] for c in remapped)
    assert "多少钱" not in joined, "议价字幕残留"
    assert "企业微信" not in joined, "高风险画面字幕残留"
    print(f"[8] 整体方案自洽：成片 {out_dur:.2f}s "
          f"(保留 {out_dur / plan.duration * 100:.0f}%)         OK")
    return plan


def test_plan_persist(plan, cfg):
    tmp = tempfile.mkdtemp(prefix="an_")
    try:
        p = os.path.join(tmp, "plan.json")
        plan.save(p)
        back = EditPlan.load(p)
        assert abs(back.output_duration() - plan.output_duration()) < 1e-6
        rep = A.write_review_report(
            plan, Config(workdir=tmp), os.path.join(tmp, "审核清单.txt"))
        with open(rep, encoding="utf-8") as f:
            txt = f.read()
        assert "议价" in txt and "高风险画面" in txt and "消音" in txt
        print("[9] plan.json + 审核清单落盘                    OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    cfg = Config(workdir="./_t3work", pause_mode="trim")
    segs = prep(cfg)
    test_customer_turn_marking(cfg, segs)
    test_mute_from_sensitive(cfg, segs)
    test_subtitle_redaction(cfg, segs)
    test_pause_plan(cfg, segs)
    test_pause_protect_qa(segs)
    test_filler(cfg, segs)
    test_speed_mode(segs)
    plan = test_full_plan_assembly(cfg, segs)
    test_plan_persist(plan, cfg)
    print("\nStep 3 分析阶段：全部通过")
