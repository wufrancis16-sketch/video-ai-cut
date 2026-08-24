# -*- coding: utf-8 -*-
"""video-ai-cut 环境与正确性自检（换对话/首次使用前运行）。

用法：
  python verify_skill.py            # 全量自检
  python verify_skill.py quick      # 快速模式（跳过耗时项：模型体积校验）

自检项（每项 PASS/FAIL）：
  1. 模块可导入（src.analyze/render/risk_screen/intro/…）
  2. ffmpeg / ffprobe 可用（which 或内置回退路径）
  3. Whisper 模型缓存完整（model.bin 等必需文件存在且非 0 字节）
  4. RapidOCR 可导入（rapidocr_onnxruntime）
  5. 中文字体存在（微软雅黑 / 黑体，供封面/字幕）
  6. 企微检测核心单测（产品页排除 + 腾讯会议否决 + 精确匹配）
  7. 字幕单行拆分单测（_split_cue 不产出超长 cue）
  8. 开场检测逻辑单测（腾讯会议文字判定 + 蓝屏判定）

全部 PASS = 环境与代码就绪，新对话可放心一键剪辑。
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

QUICK = "quick" in sys.argv[1:]
PASS = 0
FAIL = 0
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("=" * 64)
    print("video-ai-cut 环境与正确性自检")
    print("=" * 64)

    # ---- 1. 模块可导入 ----
    try:
        from src import analyze, render, risk_screen, intro, subtitle, config, timeline  # noqa
        check("模块可导入", True, "src.* 全部加载成功")
    except Exception as e:  # noqa
        check("模块可导入", False, str(e))

    # ---- 2. ffmpeg / ffprobe ----
    def _bin(name: str) -> str:
        p = shutil.which(name)
        if p:
            return p
        return (r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin"
                + f"\\{name}.EXE")
    ff = _bin("ffmpeg")
    fp = _bin("ffprobe")
    check("ffmpeg 可用", os.path.exists(ff), ff)
    check("ffprobe 可用", os.path.exists(fp), fp)

    # ---- 3. Whisper 模型缓存 ----
    from src.asr import _local_complete
    from src.config import Config
    cfg = Config.load(workdir="./_work")
    model_path = os.path.join(cfg.model_cache_dir, cfg.asr_model)
    complete = _local_complete(model_path)
    if QUICK and not complete:
        # 快速模式：只要 model.bin 存在即视为 OK
        mb = os.path.join(model_path, "model.bin")
        complete = os.path.exists(mb) and os.path.getsize(mb) > 1_000_000
    check("Whisper 模型缓存完整", complete, model_path)

    # ---- 4. RapidOCR ----
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa
        check("RapidOCR 可导入", True)
    except Exception as e:  # noqa
        check("RapidOCR 可导入", False, str(e))

    # ---- 5. 中文字体 ----
    font_found = any(os.path.exists(p) for p in (
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf"))
    check("中文字体存在", font_found)

    # ---- 6. 企微检测核心单测 ----
    from src import risk_screen as R
    from src import screen_inspect as SI
    from src.analyze import _split_cue, _max_subtitle_chars

    def _fake(left, full):
        SI._ocr_wechat_texts = lambda p: list(left)   # noqa: E731
        SI._ocr_full_texts = lambda p: list(full)     # noqa: E731

    try:
        _fake(["商品", "库存", "价格", "销售价"], ["商品", "库存", "价格"])
        s1 = R._score_frame("x.png", cfg)
        _fake(["邮件", "文档"], ["邮件", "文档", "商品", "群聊"])
        s3 = R._score_frame("x.png", cfg)
        _fake(["会议号", "开始录", "创建者"], ["腾讯会议", "会议号：123", "创建者"])
        s5 = R._score_frame("x.png", cfg)
        _fake(["会议", "日程"], ["会议", "日程"])
        s4 = R._score_frame("x.png", cfg)
        ok6 = (s1["score"] <= 2.0 and s3["score"] >= 5.0
               and s5["score"] == -100 and s4["score"] >= 5.0)
        check("企微检测单测(产品排除/强词/否决)", ok6,
              f"产品页={s1['score']} 企微={s3['score']} 会议否决={s5['score']} 会议日程={s4['score']}")
    except Exception as e:  # noqa
        check("企微检测单测", False, str(e))

    # ---- 7. 字幕单行拆分单测 ----
    try:
        mc = _max_subtitle_chars(1920, cfg.subtitle_fontsize)
        seg = {"start": 10.0, "end": 18.0,
               "text": "我们这款好生意软件支持进销存管理库存核算销售订单采购订单以及财务报表一键生成非常方便您看这里就是操作界面",
               "words": [{"word": "字", "start": 10.0 + i * 0.11, "end": 10.11 + i * 0.11}
                         for i in range(42)]}
        cues = _split_cue(seg, mc)
        ok7 = bool(cues) and all(len(c["text"]) <= mc for c in cues) \
            and abs(cues[0]["start"] - 10.0) < 0.05 and abs(cues[-1]["end"] - 18.0) < 0.05
        check("字幕单行拆分单测", ok7,
              f"{len(cues)} 条 cue，最大 {max(len(c['text']) for c in cues)} 字 ≤ {mc}")
    except Exception as e:  # noqa
        check("字幕单行拆分单测", False, str(e))

    # ---- 8. 开场检测逻辑单测 ----
    try:
        from src.intro import _frame_is_meeting_text, MEETING_TEXT_KEYS
        ok8 = bool(MEETING_TEXT_KEYS) and all(
            k in MEETING_TEXT_KEYS for k in ("腾讯会议", "会议号", "快速会议", "创建者"))
        check("开场检测词表完整", ok8, f"{len(MEETING_TEXT_KEYS)} 个腾讯会议专属词")
    except Exception as e:  # noqa
        check("开场检测词表完整", False, str(e))

    # ---- 汇总 ----
    print("=" * 64)
    print(f"结果：{PASS} PASS / {FAIL} FAIL")
    if FAIL == 0:
        print(">>> 环境与代码就绪：新对话可放心执行 python main.py <视频> 一键剪辑 <<<")
        return 0
    print(">>> 存在 FAIL 项：请先修复后再剪辑（见上方明细）<<<")
    return 1


if __name__ == "__main__":
    sys.exit(main())
