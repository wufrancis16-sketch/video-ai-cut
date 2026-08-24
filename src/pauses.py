"""停顿检测与视频编辑计划。

规则：
  停顿 < keep_threshold(1s)   : 保持原速
  停顿 1~3s                  : 加速到 speed_factor(2x)
  停顿 > speed_threshold(3s) : 直接删除

同时把「包含敏感信息的语音段」在音频上替换为哔声。

输出一段 FFmpeg filter_complex 字符串，对：
  [0:v] 已烧录字幕的视频
  [1:a] 原始音频
  [2:a] 哔声提示音
进行拼接，得到紧凑且已消音的最终音视频。
"""
from __future__ import annotations

from typing import List, Dict, Any

from .config import Config


def build_edit_plan(segments: List[Dict[str, Any]], duration: float,
                    cfg: Config) -> List[Dict[str, Any]]:
    """根据语音段间隔生成编辑片段列表。"""
    pieces: List[Dict[str, Any]] = []
    last_end = 0.0

    for seg in segments:
        s, e = seg["start"], seg["end"]
        if s > last_end + 1e-3:
            gap = s - last_end
            pieces.append(_gap_piece(last_end, s, gap, cfg))
        # 语音段：敏感则标记，用于音频替换哔声
        pieces.append({
            "kind": "speech", "start": s, "end": e,
            "speed": 1.0, "sensitive": bool(seg.get("sensitive", False)),
        })
        last_end = e

    # 末尾静音
    tail = duration - last_end
    if tail > 1e-3:
        pieces.append(_gap_piece(last_end, duration, tail, cfg))

    return [p for p in pieces if p is not None]


def _gap_piece(start: float, end: float, gap: float, cfg: Config):
    if gap > cfg.pause_speed_threshold:
        return None  # 删除
    speed = cfg.pause_speed_factor if gap > cfg.pause_keep_threshold else 1.0
    return {"kind": "silence", "start": start, "end": end,
            "speed": speed, "sensitive": False}


def build_filter_complex(pieces: List[Dict[str, Any]], cfg: Config,
                         video_in: str = "0:v", audio_in: str = "1:a",
                         beep_in: str = "2:a",
                         video_filter: str = "") -> str:
    """生成 filter_complex 文本。

    优化点（避免 Windows 命令行长度限制）：
      - 字幕等视频首部滤镜**只应用一次**，再用 split 复用给所有 trim 段，
        而不是在每个片段重复拼接 subtitles='...'。
      - 滤镜图本身通过 -filter_complex_script 从文件读取（见 pipeline.py），
        进一步规避 cmd 长度上限。

    约定输入索引：
      0 -> 视频
      1 -> 原始音频
      2 -> 哔声
    """
    n = len(pieces)
    if n == 0:
        return ""

    v_parts: List[str] = []
    a_parts: List[str] = []
    concat_inputs: List[str] = []

    # 视频：先把可选滤镜(如字幕)应用到整段，再用 split 拆成 n 路复用
    if video_filter:
        v_parts.append(f"[{video_in}]{video_filter}[vsrc]")
        src = "[vsrc]"
    else:
        src = f"[{video_in}]"
    vlabels = " ".join(f"[v{i}]" for i in range(n))
    v_parts.append(f"{src}split={n}{vlabels}")

    for i, p in enumerate(pieces):
        S = f"{p['start']:.3f}"
        E = f"{p['end']:.3f}"
        d = p["end"] - p["start"]
        sp = p["speed"]

        # 视频：从 split 出的对应标签裁剪 + 调速
        setpts = f"(1/{sp})*(PTS-STARTPTS)" if sp != 1.0 else "(PTS-STARTPTS)"
        v_parts.append(f"[v{i}]trim=start={S}:end={E},setpts={setpts}[ov{i}]")

        # 音频：统一采样格式，避免 concat 因 sample_fmt 不一致报错
        afmt = ",aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono"
        if p["kind"] == "speech" and p["sensitive"]:
            # 用哔声替换整段音频
            a_parts.append(
                f"[{beep_in}]atrim=0:{d:.3f},asetpts=PTS-STARTPTS{afmt}[a{i}]"
            )
        elif p["kind"] == "speech":
            atempo = f",atempo={sp}" if sp != 1.0 else ""
            a_parts.append(
                f"[{audio_in}]atrim=start={S}:end={E},asetpts=PTS-STARTPTS{atempo}{afmt}[a{i}]"
            )
        else:  # silence
            atempo = f",atempo={sp}" if sp != 1.0 else ""
            a_parts.append(
                f"[{audio_in}]atrim=start={S}:end={E},asetpts=PTS-STARTPTS{atempo},"
                f"volume=0{afmt}[a{i}]"
            )

        concat_inputs.append(f"[ov{i}]")
        concat_inputs.append(f"[a{i}]")

    concat = "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[outv][outa]"
    parts = v_parts + a_parts + [concat]
    return ";".join(parts)
