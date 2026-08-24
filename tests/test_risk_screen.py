"""Step 5 验证（v7 适配版 2026-08-21）：企业微信高风险画面检测。

覆盖：
  A. candidate_windows：ASR 关键词 -> 候选窗口（±keyword_pad），合并重叠
     （v7 全片扫描已不依赖它，但函数保留，纯逻辑回归）
  B. _expand_runs：从评分样本分出 delete/review 段并扩展边界（v7 API）
  C. detect 端到端（合成视频 + OCR 桩）：全片扫描 + 精确匹配 + 产品页排除
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import risk_screen as R, screen_inspect as SI  # noqa: E402
from src.config import Config  # noqa: E402
from src.utils import ffmpeg_available, run  # noqa: E402


# ---------------------------------------------------------------------------
# A. 候选窗口（v7 保留函数，纯逻辑回归）
# ---------------------------------------------------------------------------
def test_candidate_windows(cfg):
    segs = [
        {"start": 1.0, "end": 3.0, "text": "先讲功能"},
        {"start": 80.0, "end": 84.0, "text": "我给您看一下企业微信里的交付群"},
        {"start": 200.0, "end": 203.0, "text": "这是微信聊天记录"},
    ]
    wins = R.candidate_windows(segs, 240.0, cfg)
    assert (72.0, 92.0) in wins, wins
    assert (192.0, 211.0) in wins, wins
    print(f"[A1] 候选窗口：{len(wins)} 个（关键词定位 ±"
          f"{cfg.risk_screen_keyword_pad:.0f}s）        OK")


def test_candidate_windows_merge(cfg):
    segs = [
        {"start": 5.0, "end": 7.0, "text": "客户群在这里"},
        {"start": 12.0, "end": 14.0, "text": "企业微信也看一下"},
    ]
    wins = R.candidate_windows(segs, 100.0, cfg)
    assert len(wins) == 1, wins
    assert abs(wins[0][0] - 0.0) < 1e-6 and abs(wins[0][1] - 22.0) < 1e-6, wins
    print(f"[A2] 重叠候选合并为 1 段 [0, 22]             OK")


# ---------------------------------------------------------------------------
# B. 边界扩展（v7 _expand_runs：样本直接给定 score）
# ---------------------------------------------------------------------------
def _samples_v7(times, score_of):
    """times: 时间列表；score_of(t) -> score。"""
    return [{"t": t, "score": score_of(t), "reason": "x" if score_of(t) >= 3 else ""}
            for t in times]


def _grid(lo, hi, step=0.5):
    out = []
    t = lo
    while t <= hi + 1e-9:
        out.append(round(t, 3))
        t += step
    return out


def test_expand_delete_full(cfg):
    ts = _grid(70.0, 100.0)
    dec, rev = R._expand_runs(_samples_v7(ts, lambda t: 9.0 if 78.0 <= t <= 88.0 else 0.0),
                              "x.mp4", 200.0, cfg)
    assert len(dec) == 1 and len(rev) == 0, (dec, rev)
    d = dec[0]
    # 边界 = 命中极端帧 ±(pad + REFINE_BUF) = ±(1.0+1.2)
    assert abs(d["start"] - 75.8) < 0.3, d
    assert abs(d["end"] - 90.2) < 0.3, d
    print(f"[B1] 完整区间删除：[{d['start']:.1f}, {d['end']:.1f}]"
          f"（±pad+REFINE_BUF 扩展）        OK")


def test_expand_review_grade(cfg):
    # 3~5 分 → review（不自动删）
    ts = _grid(70.0, 100.0)
    dec, rev = R._expand_runs(_samples_v7(ts, lambda t: 3.5 if 78.0 <= t <= 88.0 else 0.0),
                              "x.mp4", 200.0, cfg)
    assert len(dec) == 0 and len(rev) == 1, (dec, rev)
    print(f"[B2] 中置信(3~5) -> review [{rev[0]['start']:.1f}, "
          f"{rev[0]['end']:.1f}]（不自动删）     OK")


def test_expand_short_downgrade(cfg):
    # 过短（扩展后 < min_screen）：删除级 -> 降级 review；review 级 -> 丢弃。
    # 单样本扩展段长 = 2*(pad+REFINE_BUF) = 2*(1.0+1.2) = 4.4s，故把 min_screen
    # 临时调大到 5.0 制造"过短"。
    cfg2 = Config(workdir="./_t5work", sensitive_screen_mode="heuristic",
                  risk_screen_sample_step=0.5, risk_screen_min_screen=5.0,
                  sensitive_screen_pad=1.0, sensitive_screen_conf_thr=0.6)
    dec, rev = R._expand_runs(_samples_v7([78.0], lambda t: 9.0), "x.mp4", 200.0, cfg2)
    assert len(dec) == 0 and len(rev) == 1, (dec, rev)
    dec2, rev2 = R._expand_runs(_samples_v7([78.0], lambda t: 3.5), "x.mp4", 200.0, cfg2)
    assert len(dec2) == 0 and len(rev2) == 0, (dec2, rev2)
    print("[B3] 过短：删除级降 review / review 级丢弃（防误删闪帧）     OK")


def test_expand_none(cfg):
    ts = _grid(70.0, 100.0)
    dec, rev = R._expand_runs(_samples_v7(ts, lambda t: 0.0), "x.mp4", 200.0, cfg)
    assert len(dec) == 0 and len(rev) == 0, (dec, rev)
    print("[B4] 无高风险样本 -> 不删不审     OK")


# ---------------------------------------------------------------------------
# C. 端到端 detect（合成视频 + OCR 桩：验证 v7 全片扫描 + 精确匹配 + 产品页排除）
# ---------------------------------------------------------------------------
def _make_video(path, dur=60.0):
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc=size=320x240:rate=30:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", path])


def _patch_ocr(left_list, full_list):
    """把 _score_frame 用的两个 OCR 函数替换为脚本桩。"""
    SI._ocr_wechat_texts = lambda img_path: list(left_list)   # noqa: E731
    SI._ocr_full_texts = lambda img_path: list(full_list)     # noqa: E731


def test_detect_wechat_found(cfg, tmp):
    if not ffmpeg_available():
        print("[C] 跳过：环境无 ffmpeg")
        return
    src = os.path.join(tmp, "risk.mp4")
    _make_video(src, dur=60.0)
    # 全片都是企微左栏（邮件/文档/日程/会议 精确命中）→ 应产出 delete 段
    _patch_ocr(["消息", "邮件", "文档", "日程", "待办", "会议"],
               ["企业微信", "外部群", "联系人"])
    res = R.detect(src, [], None, cfg)
    dels = res.get("delete", [])
    assert dels, res
    d = dels[0]
    assert 55.0 <= d["end"] <= 60.5, d      # 覆盖全片（60s - 尾部缓冲）
    print(f"[C1] 企微界面全片检出：[{d['start']:.1f}, {d['end']:.1f}]"
          f"（全片扫描 + 精确匹配）         OK")


def test_detect_product_page_kept(cfg, tmp):
    if not ffmpeg_available():
        print("[C] 跳过：环境无 ffmpeg")
        return
    src = os.path.join(tmp, "product.mp4")
    _make_video(src, dur=60.0)
    # 好生意产品页：左栏无强词，含 商品/库存/价格 → 产品页排除 → 不删
    _patch_ocr(["首页", "商品", "库存", "客户中心", "销售管理"],
               ["商品", "库存", "价格", "销售价"])
    res = R.detect(src, [], None, cfg)
    assert res.get("delete") == [], res
    print("[C2] 好生意产品页 -> 0 删除（产品页排除生效）     OK")


if __name__ == "__main__":
    cfg = Config(workdir="./_t5work", sensitive_screen_mode="heuristic")
    cfg.risk_screen_sample_step = 0.5
    cfg.risk_screen_max_expand = 60.0
    cfg.risk_screen_keyword_pad = 8.0
    cfg.risk_screen_min_screen = 1.0
    cfg.sensitive_screen_pad = 1.0
    cfg.sensitive_screen_conf_thr = 0.6

    tmp = tempfile.mkdtemp(prefix="t5_")
    try:
        test_candidate_windows(cfg)
        test_candidate_windows_merge(cfg)
        test_expand_delete_full(cfg)
        test_expand_review_grade(cfg)
        test_expand_short_downgrade(cfg)
        test_expand_none(cfg)
        test_detect_wechat_found(cfg, tmp)
        test_detect_product_page_kept(cfg, tmp)
        print("\nStep 5 高风险画面检测（v7）：全部通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
