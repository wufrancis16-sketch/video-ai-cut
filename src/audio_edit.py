"""采样级精确的音频剪辑（纯标准库，无第三方依赖）。

为什么不用 ffmpeg 的 aselect / atrim+concat：
1. aselect 只能按「音频帧」（约 1024 采样 ≈ 21ms）整体丢弃，无法与视频的
   1/fps 帧栅格对齐；几十上百个切点后会累积成可感知的音画不同步。
2. atrim + concat 需要 asplit 成 N 路，N 很大时数据被复制 N 份，
   内存与耗时都不可接受。

本模块直接在 PCM 采样上操作：
- 消音段：原声置零并混入正弦「哔」提示音（带淡入淡出，避免爆音）
- 删除：按保留区间拼接采样（**采样级精确**）
- 片头封面：前置精确长度的静音
拼接处加 4ms 淡入淡出，消除硬切产生的「咔哒」声。

时长保证：保留区间由 timeline 对齐到 1/fps 栅格，因此
    音频时长 = Σ(区间长度) = 视频保留帧数 / fps
两者严格相等 —— 这是音画同步的根本保证。
"""
from __future__ import annotations

import math
import wave
from array import array
from typing import List, Optional, Sequence, Tuple

Range = Tuple[float, float]

FADE_MS = 4.0          # 拼接处淡入淡出
BEEP_FADE_MS = 8.0     # 哔声淡入淡出


def _read_wav(path: str):
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise RuntimeError(
                f"仅支持 16bit PCM（当前 {wf.getsampwidth() * 8}bit）：{path}")
        ch = wf.getnchannels()
        sr = wf.getframerate()
        data = wf.readframes(wf.getnframes())
    buf = array("h")
    buf.frombytes(data)
    return buf, ch, sr


def _write_wav(path: str, buf: array, ch: int, sr: int) -> str:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf.tobytes())
    return path


def _clamp_idx(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))


def _apply_beep(buf: array, ch: int, sr: int, ranges: Sequence[Range],
                freq: float, volume: float) -> int:
    """把 ranges 内的原声替换为哔声。返回实际处理的段数。"""
    total = len(buf) // ch
    amp = int(max(0.0, min(volume, 1.0)) * 32767 * 0.9)
    fade_n = max(1, int(sr * BEEP_FADE_MS / 1000.0))
    done = 0
    for s, e in ranges:
        i0 = _clamp_idx(int(round(s * sr)), 0, total)
        i1 = _clamp_idx(int(round(e * sr)), 0, total)
        n = i1 - i0
        if n <= 0:
            continue
        w = 2.0 * math.pi * freq / sr
        for k in range(n):
            # 淡入淡出包络，避免哔声首尾爆音
            env = 1.0
            if k < fade_n:
                env = k / fade_n
            elif k > n - fade_n:
                env = max(0.0, (n - k) / fade_n)
            v = int(amp * env * math.sin(w * k))
            base = (i0 + k) * ch
            for c in range(ch):
                buf[base + c] = v
        done += 1
    return done


def _fade_edge(buf: array, ch: int, start: int, count: int,
               fade_in: bool) -> None:
    """对 [start, start+count) 采样做线性淡入/淡出（原地）。"""
    if count <= 0:
        return
    for k in range(count):
        g = (k / count) if fade_in else (1.0 - k / count)
        base = (start + k) * ch
        for c in range(ch):
            buf[base + c] = int(buf[base + c] * g)


def _resample_segment(buf: array, ch: int, factor: float) -> array:
    """对单段音频做线性重采样（factor>1 表示加速，输出更短）。

    纯标准库实现、无 numpy。加速会伴随轻微音高变化（与视频变速一致），
    但保证音频时长与变速后的视频严格对齐——音画同步优先于音质。
    """
    n = len(buf) // ch
    m = max(1, int(round(n / factor)))
    if m == n:
        return buf
    out = array("h")
    for c in range(ch):
        src = [buf[k * ch + c] for k in range(n)]
        for j in range(m):
            pos = (j * (n - 1) / (m - 1)) if m > 1 else 0.0
            i0 = int(pos)
            i1 = min(i0 + 1, n - 1)
            frac = pos - i0
            v = src[i0] * (1.0 - frac) + src[i1] * frac
            out.append(int(round(v)))
    return out


def render_audio(src_wav: str, out_wav: str,
                 keep_ranges: Sequence[Range],
                 mute_ranges: Sequence[Range] = (),
                 beep_freq: float = 1000.0,
                 beep_volume: float = 0.2,
                 lead_silence: float = 0.0,
                 expect_duration: Optional[float] = None,
                 speed_pieces: Optional[Sequence[Tuple[float, float, float]]] = None
                 ) -> dict:
    """按剪辑方案生成成片音频。

    参数：
      keep_ranges   保留区间（原始时间，已对齐帧栅格）
      mute_ranges   消音区间（原始时间）
      lead_silence  片头封面对应的静音长度（秒）
      expect_duration  期望输出时长；提供时会补齐/裁剪到精确样本数，
                       用于与视频帧数严格对齐
      speed_pieces  变速片段 [(start,end,speed),...]（原始时间）。提供时按
                    各片段 speed 对音频做重采样，使音频时长与变速后的视频
                    严格一致（音画同步优先于音质）。

    注意：消音/哔声在**原始时间轴**上先完成，再做变速重采样，顺序不可颠倒。
    """
    buf, ch, sr = _read_wav(src_wav)
    total = len(buf) // ch

    # 1) 消音 + 哔声（在原始时间轴上操作）
    beeped = _apply_beep(buf, ch, sr, mute_ranges, beep_freq, beep_volume)

    # 2) 按片段拼接（含变速重采样）
    if speed_pieces:
        pieces: Sequence[Tuple[float, float, float]] = list(speed_pieces)
    else:
        pieces = [(s, e, 1.0) for (s, e) in keep_ranges]

    out = array("h")
    if lead_silence > 1e-6:
        out.extend(array("h", [0]) * (int(round(lead_silence * sr)) * ch))
    joins: List[int] = []
    for s, e, sp in pieces:
        i0 = _clamp_idx(int(round(s * sr)), 0, total)
        i1 = _clamp_idx(int(round(e * sr)), 0, total)
        if i1 <= i0:
            continue
        seg = buf[i0 * ch:i1 * ch]
        if sp != 1.0 and (i1 - i0) > ch:
            seg = _resample_segment(seg, ch, float(sp))
        joins.append(len(out) // ch)
        out.extend(seg)

    # 3) 拼接处淡入淡出，消除硬切爆音
    fade_n = max(1, int(sr * FADE_MS / 1000.0))
    n_out = len(out) // ch
    for j in joins:
        if j > 0:
            _fade_edge(out, ch, max(0, j - fade_n), min(fade_n, j), False)
        _fade_edge(out, ch, j, min(fade_n, n_out - j), True)
    if n_out > fade_n:
        _fade_edge(out, ch, n_out - fade_n, fade_n, False)   # 结尾淡出

    # 4) 与视频严格对齐：补齐/裁剪到期望样本数
    aligned = False
    if expect_duration is not None and expect_duration > 0:
        want = int(round(expect_duration * sr))
        cur = len(out) // ch
        if want > cur:
            out.extend(array("h", [0]) * ((want - cur) * ch))
            aligned = True
        elif want < cur:
            del out[want * ch:]
            aligned = True

    _write_wav(out_wav, out, ch, sr)
    return {
        "path": out_wav,
        "sample_rate": sr,
        "channels": ch,
        "duration": (len(out) // ch) / sr,
        "mute_applied": beeped,
        "joins": len(joins),
        "aligned": aligned,
    }


def make_silence(out_wav: str, duration: float, sr: int = 48000,
                 ch: int = 2) -> str:
    """生成静音 WAV（无音轨视频时用于占位，保证成片一定有音轨）。"""
    n = int(round(duration * sr))
    buf = array("h", [0]) * (n * ch)
    return _write_wav(out_wav, buf, ch, sr)


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())
