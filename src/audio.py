"""音频相关：提取音频、生成哔声提示音。"""
from __future__ import annotations

from .utils import run


def extract_audio(video_path: str, out_wav: str, sr: int = 44100, mono: bool = True) -> str:
    """从视频中提取单声道 WAV 音频。"""
    cmd = [
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-acodec", "pcm_s16le", "-ar", str(sr),
        "-ac", "1" if mono else "2", out_wav,
    ]
    run(cmd)
    return out_wav


def generate_beep(out_wav: str, duration: float = 10.0, freq: float = 1000.0,
                  sr: int = 44100) -> str:
    """生成一段正弦波提示音 (默认 10 秒，足够覆盖多数敏感片段)。"""
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-ac", "1", "-ar", str(sr), out_wav,
    ]
    run(cmd)
    return out_wav


def extract_frame(video_path: str, out_image: str, ts: float = 0.0) -> str:
    """抽取指定时间点的视频帧作为图片。"""
    cmd = [
        "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", video_path,
        "-frames:v", "1", "-q:v", "2", out_image,
    ]
    run(cmd)
    return out_image


def slice_video(video_path: str, out_dir: str, chunk_seconds: int) -> List[str]:
    """用 segment 复用器无损切片（不重新编码），按 chunk_seconds 切分。

    输出 out_dir/chunk_0000.mp4, chunk_0001.mp4 ...（编号从 0 开始）。
    返回排序后的分片路径列表。
    """
    import os
    from .utils import ensure_dir, run

    ensure_dir(out_dir)
    pattern = os.path.join(out_dir, "chunk_%04d.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1", "-c", "copy", pattern,
    ]
    run(cmd)
    files = sorted(
        f for f in os.listdir(out_dir)
        if f.startswith("chunk_") and f.endswith(".mp4")
    )
    return [os.path.join(out_dir, f) for f in files]
