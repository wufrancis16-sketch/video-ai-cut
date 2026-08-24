"""通用工具：FFmpeg 调用、媒体探测、时间格式。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess


def run(cmd, check=True, **kw):
    """执行命令，返回 CompletedProcess。

    注意：Windows 下 subprocess 的 text 模式默认按本地编码(GBK)解码，
    遇到 UTF-8 的中文路径/输出会抛 UnicodeDecodeError。显式指定
    encoding=utf-8 + errors=replace 避免崩溃。
    """
    print("+ " + (cmd if isinstance(cmd, str) else " ".join(cmd)), flush=True)
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kw
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"命令失败 (code={proc.returncode}):\n{proc.stderr}")
    return proc


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


_FILTER_COMPLEX_SCRIPT_OK = None


def ffmpeg_supports_filter_complex_script() -> bool:
    """探测当前 ffmpeg 是否支持 -filter_complex_script（规避 Windows 命令行长度上限）。

    部分构建（如 ffmpeg 9.0-full_build）未包含该选项；此时回退为内联
    -filter_complex。新架构使用 select 表达式（天然紧凑），常规视频的滤镜图
    远小于 8191 字符上限，内联即可。
    """
    global _FILTER_COMPLEX_SCRIPT_OK
    if _FILTER_COMPLEX_SCRIPT_OK is None:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-h"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30)
            _FILTER_COMPLEX_SCRIPT_OK = "filter_complex_script" in proc.stdout
        except Exception:  # noqa
            _FILTER_COMPLEX_SCRIPT_OK = False
    return _FILTER_COMPLEX_SCRIPT_OK


def ffprobe_json(path: str) -> dict:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = run(cmd)
    return json.loads(proc.stdout)


def get_duration(path: str) -> float:
    info = ffprobe_json(path)
    return float(info["format"]["duration"])


def get_resolution(path: str) -> tuple[int, int]:
    info = ffprobe_json(path)
    for s in info["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError("未找到视频流")


def _parse_rate(v: str) -> float:
    """解析 ffprobe 的 '30000/1001' 形式帧率。"""
    try:
        if not v:
            return 0.0
        if "/" in v:
            a, b = v.split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(v)
    except Exception:  # noqa
        return 0.0


def probe_video(path: str) -> dict:
    """一次探测拿到渲染所需的全部参数。

    fps 优先取 avg_frame_rate（更能代表实际平均帧率），异常时回退
    r_frame_rate，再回退 30。分辨率/帧率必须原样保留到成片，
    因此这里的值会直接决定最终编码参数。
    """
    info = ffprobe_json(path)
    v = None
    a = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    if v is None:
        raise RuntimeError(f"未找到视频流: {path}")

    fps = _parse_rate(v.get("avg_frame_rate", "")) or \
        _parse_rate(v.get("r_frame_rate", "")) or 30.0
    if not (1.0 <= fps <= 240.0):
        fps = 30.0

    try:
        duration = float(info["format"]["duration"])
    except Exception:  # noqa
        duration = _parse_rate(v.get("duration", "")) or 0.0

    return {
        "duration": duration,
        "fps": fps,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "vcodec": v.get("codec_name", ""),
        "has_audio": a is not None,
        "sample_rate": int(a.get("sample_rate") or 48000) if a else 0,
        "channels": int(a.get("channels") or 1) if a else 0,
        "acodec": a.get("codec_name", "") if a else "",
    }


def has_audio_stream(path: str) -> bool:
    try:
        return probe_video(path)["has_audio"]
    except Exception:  # noqa
        return False


def ass_timecode(seconds: float) -> str:
    """将秒转换为 ASS 时间码 H:MM:SS.cc"""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def safe_subtitle_path(path: str) -> str:
    """转义字幕文件路径，使其可被 FFmpeg subtitles 滤镜安全使用。

    在单引号包裹的 filter 值内，冒号为字面量，无需转义；仅需把反斜杠
    转为正斜杠、转义单引号即可（Windows 路径 C:/... 的冒号保持原样）。
    """
    p = path.replace("\\", "/").replace("'", "\\'")
    return f"'{p}'"


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def concat_videos(video_list: list, out_path: str):
    """合并多个 mp4（分片 / 批次子片段）。

    关键点：绝不使用 concat 解复用器(-f concat)直接拼 mp4——该方式在
    跨文件拼接 H.264 时会因参数集/MOOV 问题报 h264_mp4toannexb 失败、
    静默产出空文件。这里改用 concat *滤镜*：每个输入被独立打开解码，
    再在 filtergraph 内拼接，彻底规避该问题，且对输出做流校验。
    """
    videos = [os.path.abspath(v) for v in video_list]
    n = len(videos)
    if n == 0:
        raise RuntimeError("concat_videos: 空输入列表")

    # 单文件：直接重新封装/编码
    if n == 1:
        cmd = ["ffmpeg", "-y", "-i", videos[0],
               "-c:v", "libx264", "-crf", "20", "-preset", "medium",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
               out_path]
        run(cmd)
        _verify_streams(out_path)
        return out_path

    # 多文件：concat 滤镜
    inputs = []
    for v in videos:
        inputs += ["-i", v]
    labels = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    fc = f"{labels}concat=n={n}:v=1:a=1[outv][outa]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        out_path,
    ]
    run(cmd)
    _verify_streams(out_path)
    return out_path


def _verify_streams(path: str):
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of",
             "default=noprint_wrappers=1", path],
            capture_output=True, text=True, encoding="utf-8",
        )
        if "video" not in proc.stdout:
            raise RuntimeError(f"合并失败：输出文件无视频流 {path}")
    except FileNotFoundError:
        raise RuntimeError(f"合并失败：输出文件不存在 {path}")
