"""生成 ASS 字幕文件 (含脱敏文本)，适合短视频底部居中显示。"""
from __future__ import annotations

from typing import List, Dict, Any

from .config import Config
from .utils import ass_timecode


def build_ass(segments: List[Dict[str, Any]], width: int, height: int,
              cfg: Config) -> str:
    margin_v = int(height * 0.08)
    margin_lr = int(width * 0.05)
    fontsize = cfg.subtitle_fontsize
    font = cfg.subtitle_font

    lines = []
    lines.append("[Script Info]")
    lines.append(f"PlayResX: {width}")
    lines.append(f"PlayResY: {height}")
    lines.append("WrapStyle: 2")
    lines.append("ScaledBorderAndShadow: yes")
    lines.append("")
    lines.append("[V4+ Styles]")
    lines.append("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
                 "Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV")
    lines.append(
        f"Style: Default,{font},{fontsize},{cfg.subtitle_primary},"
        f"{cfg.subtitle_outline},1,3,1,2,{margin_lr},{margin_lr},{margin_v}"
    )
    lines.append("")
    lines.append("[Events]")
    lines.append("Format: Layer, Start, End, Style, Text")

    for seg in segments:
        text = seg.get("redacted_text", seg["text"]).replace("\n", " ").strip()
        # 转义 ASS 特殊字符
        text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        text = text.replace(",", "，")
        if text:
            lines.append(
                f"Dialogue: 0,{ass_timecode(seg['start'])},"
                f"{ass_timecode(seg['end'])},Default,{text}"
            )
    return "\n".join(lines)


def write_ass(segments: List[Dict[str, Any]], width: int, height: int,
              cfg: Config, out_path: str) -> str:
    content = build_ass(segments, width, height, cfg)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def _srt_timecode(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments: List[Dict[str, Any]]) -> str:
    """segments 需已包含所有字段；text 使用 redacted_text（脱敏后）。"""
    lines = []
    idx = 1
    for seg in segments:
        text = seg.get("redacted_text", seg["text"]).replace("\n", " ").strip()
        text = text.replace(",", "，")
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{_srt_timecode(seg['start'])} --> {_srt_timecode(seg['end'])}")
        lines.append(text)
        lines.append("")
        idx += 1
    return "\n".join(lines)


def write_srt(segments: List[Dict[str, Any]], out_path: str) -> str:
    content = build_srt(segments)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
