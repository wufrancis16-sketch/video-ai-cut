"""检测原视频是否已含字幕，用于跳过字幕识别与烧录（避免重复叠加字幕）。

两种判定：
  1) 内嵌字幕流（embedded）：ffprobe 直接读取容器中的 subtitle 流，零成本、最可靠。
  2) 硬字幕（burned）：OCR 视频底部字幕带，保守判定（需多个采样点命中且文字
     内容跨采样变化，以排除静止 UI 底栏误判）。

命中后调用方应跳过 ASR→字幕的生成与烧录；硬字幕本身已烧在画面像素中，
重编码后天然保留。内嵌字幕流因无法自动重排到剪辑后时间轴，故选择丢弃
（不复制、不烧新）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import screen_inspect as _si
from .config import Config
from .utils import ffprobe_json, ensure_dir

# 底部字幕带（相对整帧高度比例）：通常位于画面最下方 22%
_BAND_TOP = 0.78
# 水平居中容差：排除最左/最右的软件导航边栏，只认居中字幕
_CX_LO = 0.15
_CX_HI = 0.85


def detect_existing_subtitle(video: str, cfg: Config,
                             duration: float = 0.0) -> Optional[Dict[str, Any]]:
    """判断原视频是否已含字幕。

    返回 None 表示未检测到（按常规流程识别并烧录字幕）；否则返回
    {"type": "embedded", "codec":..., "lang":..., "count":...}
    或 {"type": "burned", "samples":..., "hits":...}，调用方据此跳过字幕生成。
    """
    # 1) 内嵌字幕流：ffprobe 直接读取，零成本且最可靠
    subs = _probe_subtitle_streams(video)
    if subs:
        s0 = subs[0]
        tags = s0.get("tags", {}) or {}
        return {
            "type": "embedded",
            "codec": s0.get("codec_name", ""),
            "lang": tags.get("language", "") or s0.get("language", ""),
            "count": len(subs),
        }

    # 2) 硬字幕：OCR 底部字幕带（仅当开启「跳过」且确实需要烧录时）
    if (cfg.skip_subtitle_if_exists and cfg.burn_subtitle
            and cfg.detect_burned_subtitle and duration > 4):
        return _detect_burned_subtitle(video, cfg, duration)
    return None


def _probe_subtitle_streams(video: str) -> List[Dict[str, Any]]:
    try:
        info = ffprobe_json(video)
    except Exception:  # noqa
        return []
    return [s for s in info.get("streams", [])
            if s.get("codec_type") == "subtitle"]


def _ocr_bottom_band(img_path: str) -> List[str]:
    """对底部字幕带做 OCR，返回位于底部居中区域的中文/文本片段列表。"""
    try:
        from PIL import Image
        import numpy as np
    except Exception:  # noqa
        return []
    try:
        img = Image.open(img_path).convert("RGB")
    except Exception:  # noqa
        return []
    w, h = img.size
    top = int(h * _BAND_TOP)
    band = img.crop((0, top, w, h))
    # 放大保证小字号可读
    target_h = 600
    scale = min(4.0, max(2.0, target_h / float(band.height)))
    band = band.resize((int(band.width * scale), int(band.height * scale)),
                       Image.LANCZOS)
    arr = np.array(band).astype(np.uint8)
    try:
        ocr = _si._get_ocr()
        result, _ = ocr(arr)
    except Exception:  # noqa
        return []
    if not result:
        return []
    bw, bh = band.size
    out: List[str] = []
    for box, text, _score in result:
        if not text or not text.strip():
            continue
        xs = [p[0] for p in box]
        cx = (min(xs) + max(xs)) / 2.0 / bw
        # box 的 y 是相对 band 顶部；band 本身已在画面底部，命中即视为底带文字
        if _CX_LO <= cx <= _CX_HI:
            out.append(text.strip())
    return out


def _detect_burned_subtitle(video: str, cfg: Config, duration: float
                           ) -> Optional[Dict[str, Any]]:
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "subtitle_detect")
    ensure_dir(tmp)
    n = max(3, int(cfg.burned_subtitle_samples))
    lo, hi = duration * 0.08, duration * 0.92
    if hi <= lo + 1:
        return None
    hits = 0
    samples: List[List[str]] = []
    for i in range(n):
        t = lo + (hi - lo) * i / max(n - 1, 1)
        p = os.path.join(tmp, f"sb_{i}.png")
        try:
            if not _si._extract_frame(video, t, p, width=1280):
                continue
            texts = _ocr_bottom_band(p)
        except Exception:  # noqa
            continue
        if texts:
            hits += 1
            samples.append(texts)
    min_hits = max(2, int(cfg.burned_subtitle_min_hits))
    if hits < min_hits:
        return None
    # 文字内容跨采样有变化 -> 更像滚动字幕，而非静止 UI 底栏
    if len(samples) >= 2:
        first = set("".join(samples[0]))
        changed = any(set("".join(s)) != first for s in samples[1:])
        if not changed:
            # 所有采样文字完全一致，疑为静态 UI，不判为字幕
            return None
    return {"type": "burned", "samples": n, "hits": hits}
