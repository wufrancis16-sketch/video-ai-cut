"""语音识别：基于 faster-whisper 的本地 ASR。

设计要点（针对长视频 / 国内网络）：
1. 模型只加载一次，跨多个视频分片复用，避免每个分片重复加载大模型。
2. 模型可通过本地目录路径直接加载；若使用官方尺寸名 (tiny/base/small/...)，
   优先复用本地缓存目录，缺失时通过 requests（自动读取 HTTP(S)_PROXY 环境变量）
   下载整个仓库，规避 huggingface_hub 在代理环境下写出 0 字节文件的问题。

输出 segments 结构：
[
  {
    "start": float, "end": float, "text": str,
    "words": [{"word": str, "start": float, "end": float}, ...]
  },
  ...
]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Dict, Any, Optional

import requests


# ---------------------------------------------------------------------------
# 模型获取（requests 走系统代理，稳定可靠）
# ---------------------------------------------------------------------------
def _repo_files(repo_id: str) -> List[str]:
    api = "https://huggingface.co/api/models/" + repo_id
    mirror_api = "https://hf-mirror.com/api/models/" + repo_id
    for url in (api, mirror_api):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return [s["rfilename"] for s in r.json().get("siblings", [])]
        except Exception as e:  # noqa
            print(f"    [warn] 列举文件失败 {url}: {e}")
    raise RuntimeError("无法从 HuggingFace 列举模型文件")


def _hosts(repo_id: str, path: str):
    """返回 (主站, 镜像) 两个 resolve URL。"""
    hf = f"https://huggingface.co/{repo_id}/resolve/main/{path}"
    mirror = f"https://hf-mirror.com/{repo_id}/resolve/main/{path}"
    return hf, mirror


def _already_complete(dst: str, url: str) -> bool:
    """通过 HEAD 比对 Content-Length，判断本地文件是否已完整。"""
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        return False
    try:
        r = requests.head(url, timeout=20, allow_redirects=True)
        total = int(r.headers.get("Content-Length", 0))
        if total and os.path.getsize(dst) == total:
            return True
    except Exception:  # noqa
        pass
    return False


def _download_file(repo_id: str, path: str, dst: str, retries: int = 4):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    hf_url, mirror_url = _hosts(repo_id, path)
    last_err = None
    for attempt in range(retries):
        for url in (hf_url, mirror_url):
            tmp = dst + ".part"
            try:
                with requests.get(url, stream=True, timeout=180) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    written = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 20):
                            if chunk:
                                f.write(chunk)
                                written += len(chunk)
                    if total and written != total:
                        raise IOError(f"大小不符 {written}/{total}")
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(tmp, dst)
                    return
            except Exception as e:  # noqa
                last_err = e
                if os.path.exists(tmp):
                    os.remove(tmp)
                continue
        print(f"    [retry {attempt + 1}/{retries}] {path}: {last_err}")
    raise RuntimeError(f"下载失败 {path}: {last_err}")


def _local_complete(local: str) -> bool:
    """离线判断本地模型目录是否完整（不发起任何网络请求）。

    faster-whisper 加载一个尺寸目录至少需要 model.bin + config.json +
    tokenizer.json + vocabulary.txt。model.bin 对 small 约 483MB，用 1MB
    阈值可挡住 0 字节 / 损坏的残片文件。
    """
    if not os.path.isdir(local):
        return False
    required = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
    for name in required:
        p = os.path.join(local, name)
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return False
    if os.path.getsize(os.path.join(local, "model.bin")) < 1_000_000:
        return False
    return True


def ensure_model(model_size: str, cache_dir: str) -> str:
    """返回本地模型目录路径。model_size 为尺寸名时自动下载到缓存。"""
    # 直接传了本地目录
    if os.path.isdir(model_size):
        return model_size

    local = os.path.join(cache_dir, model_size)
    repo_id = f"Systran/faster-whisper-{model_size}"

    # 离线优先：模型已完整存在时，绝不发起网络请求（避免断网时崩溃）。
    if _local_complete(local):
        return local

    # 模型缺失/不完整，才尝试从 HuggingFace 列举并下载。
    try:
        files = _repo_files(repo_id)
    except RuntimeError:
        # 网络不可用：若本地至少存在 model.bin，则尽力回退使用，否则上抛。
        if os.path.exists(os.path.join(local, "model.bin")):
            print("    [warn] 无法连接 HuggingFace，已回退使用本地缓存模型")
            return local
        raise

    # 所有仓库文件都已完整下载才跳过（离线优先已处理绝大部分情况）
    if files and all(
        os.path.exists(os.path.join(local, p))
        and os.path.getsize(os.path.join(local, p)) > 0
        for p in files
    ):
        return local

    print(f"  [模型] 下载 {repo_id} -> {local}")
    os.makedirs(local, exist_ok=True)
    for path in files:
        dst = os.path.join(local, path)
        hf_url, mirror_url = _hosts(repo_id, path)
        if _already_complete(dst, hf_url) or _already_complete(dst, mirror_url):
            print(f"    - {path} (已完成，跳过)")
            continue
        print(f"    - {path}")
        _download_file(repo_id, path, dst)
    return local


def load_model(cfg) -> "object":
    """加载 WhisperModel 一次并返回，供后续所有分片复用。"""
    from faster_whisper import WhisperModel

    local = ensure_model(cfg.asr_model, cfg.model_cache_dir)
    compute_type = "int8" if cfg.device == "cpu" else "float16"
    print(f"  [模型] 加载 {local} (device={cfg.device}, compute={compute_type})")
    return WhisperModel(local, device=cfg.device, compute_type=compute_type)


def transcribe_with(model, audio_path: str, language: str = "zh") -> List[Dict[str, Any]]:
    """使用已加载的 model 对单个音频文件转写。"""
    segments_iter, _info = model.transcribe(
        audio_path,
        language=language,
        word_timestamps=True,
        # 2026-08-20 内存保守参数（本机 15.7GB 但可用常 <3GB，默认 beam=5 会 OOM）：
        beam_size=1,        # 贪心解码：内存减半以上、速度更快，中文准确率略降（small 模型可接受）
        vad_filter=True,    # 保持 VAD：本机视频音量低（RMS ~100/32768），关 VAD 会把语音当静音滤成 0 段。
                            # 内存安全由分片转写(_transcribe_chunked)保证：每片 300s 的 VAD context 仅 ~13MiB
    )

    results: List[Dict[str, Any]] = []
    for seg in segments_iter:
        words = []
        if seg.words:
            words = [
                {"word": w.word, "start": float(w.start), "end": float(w.end)}
                for w in seg.words
            ]
        results.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": seg.text.strip(),
            "words": words,
        })
    return results


# ---------------------------------------------------------------------------
# 缓存化 ASR（全片只跑一次，二次运行零开销）
# ---------------------------------------------------------------------------
class LazyModel:
    """按需加载 WhisperModel。

    命中 ASR 缓存时 get() 永远不会被调用，因此不会加载/下载大模型，
    这是二次运行「秒级完成分析」的关键。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._model = None

    def get(self):
        if self._model is None:
            self._model = load_model(self.cfg)
        return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        """释放 Whisper 模型，腾出内存给后续 OCR 检测。

        关键：faster-whisper 的 WhisperModel 常驻 ~0.5GB+（small 模型），
        若与 risk_screen 的 RapidOCR 同时驻留，本机可用内存常 <3GB 会触发
        OOM SIGKILL（无 Python traceback）。ASR 完成后立即 del + gc 释放，
        让 OCR 阶段只剩 RapidOCR + 帧缓冲，峰值内存大幅下降。
        """
        if self._model is not None:
            try:
                del self._model
            except Exception:  # noqa
                pass
            self._model = None
            import gc
            gc.collect()


def asr_cache_key(video_path: str, cfg) -> str:
    """ASR 缓存键。

    故意**不含 device**：CPU 与 GPU 的识别结果可直接互相复用，
    切换硬件不需要重跑 ASR。模型尺寸或语言变化才失效。
    """
    from . import cache
    return cache.signature(video_path, {
        "model": cfg.asr_model,
        "lang": cfg.asr_language,
    })


def transcribe_video_cached(video_path: str, audio, cfg,
                            model_holder: Optional["LazyModel"] = None
                            ) -> List[Dict[str, Any]]:
    """对整个视频做一次 ASR，结果写缓存；已有缓存则直接读取。

    参数：
      audio  — 音频路径(str) 或 返回音频路径的 callable。传 callable 时，
               命中缓存不会执行音频提取，省掉一次 ffmpeg 解码。
    """
    from . import cache

    key = asr_cache_key(video_path, cfg)

    def _produce():
        audio_path = audio() if callable(audio) else audio
        holder = model_holder or LazyModel(cfg)
        return _transcribe_chunked(holder.get(), audio_path, cfg)

    return cache.get_or_create(cfg, key, "asr", _produce, label="ASR 结果")


def _transcribe_chunked(model, audio_path: str, cfg) -> List[Dict[str, Any]]:
    """分片转写：整条音频的 STFT 特征一次性分配巨大（32min ≈ 592MiB complex128），
    本机可用内存常 <3GB 会 OOM。按 cfg.chunk_seconds 切片逐片转写，
    每片 STFT 峰值仅 ~100MiB，结果按时间偏移合并。
    代价：片边界约 1-2 词可能丢失（可接受，敏感/议价检测基于句级）。
    """
    chunk_sec = int(getattr(cfg, "chunk_seconds", 300) or 300)
    dur = _probe_duration(audio_path)
    if dur <= chunk_sec:
        return transcribe_with(model, audio_path, cfg.asr_language)
    ffmpeg = (shutil.which("ffmpeg") or
              r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin\ffmpeg.EXE")
    work = os.path.dirname(audio_path) or "."
    all_segs: List[Dict[str, Any]] = []
    t = 0.0
    while t < dur:
        seg_path = os.path.join(work, f"_asr_seg_{int(t)}.wav")
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}",
             "-t", f"{min(chunk_sec, dur - t):.3f}", "-i", audio_path,
             "-ar", "16000", "-ac", "1", seg_path],
            timeout=120)
        segs = transcribe_with(model, seg_path, cfg.asr_language)
        for s in segs:
            s["start"] += t
            s["end"] += t
        all_segs.extend(segs)
        try:
            os.remove(seg_path)
        except OSError:
            pass
        t += chunk_sec
    return all_segs


def _probe_duration(path: str) -> float:
    """ffprobe 取音频时长（秒）。stderr 重定向文件避免 Windows 管道死锁。"""
    import json as _json
    probe = (shutil.which("ffprobe") or
             r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin\ffprobe.EXE")
    fd, tmp_json = tempfile.mkstemp(suffix=".json", prefix="probe_")
    os.close(fd)
    try:
        with open(tmp_json, "w", encoding="utf-8") as out:
            subprocess.run([probe, "-v", "quiet", "-print_format", "json",
                            "-show_format", path],
                           stdout=out, stderr=subprocess.DEVNULL, timeout=30)
        with open(tmp_json, "r", encoding="utf-8") as f:
            d = _json.load(f)
        return float(d["format"]["duration"])
    finally:
        try:
            os.remove(tmp_json)
        except OSError:
            pass
