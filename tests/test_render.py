"""Step 4 验证：一次 FFmpeg filter_complex 最终处理。

覆盖三块：
  A. 视频 select 表达式 + 帧数数学（纯逻辑，不需 ffmpeg）
  B. 采样级精确音频剪辑（纯标准库，不需 ffmpeg）
  C. 全流程 render() 在合成视频上跑通「一次编码」+ 帧级精确删除 +
     音画严格同步（需要 ffmpeg，缺失则跳过）

关键不变量：成片视频帧数 = Σ keep 区间帧数；成片音频采样数 =
同一帧数 × sr / fps。两者严格相等 —— 这是音画同步的根本保证。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import wave
from array import array

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import audio_edit, render as R, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.timeline import EditPlan  # noqa: E402
from src.utils import ffmpeg_available, probe_video, run  # noqa: E402


# ---------------------------------------------------------------------------
# 合成音频（纯标准库，供 B 段用，不需 ffmpeg）
# ---------------------------------------------------------------------------
def make_sine_wav(path, sr=48000, ch=2, dur=10.0, freq=440.0):
    n = int(round(sr * dur))
    buf = array("h")
    for i in range(n):
        v = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sr))
        buf.extend([v] * ch)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf.tobytes())
    return path


def read_wav_samples(path):
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        data = wf.readframes(wf.getnframes())
    buf = array("h")
    buf.frombytes(data)
    return buf, ch, sr


# ===========================================================================
# A. select 表达式 / 帧数数学
# ===========================================================================
def test_select_expr_basic(cfg):
    fps = 30.0
    # 保留 [0,5) 和 [10,15)，删除 [5,10)
    keep = [(0.0, 5.0), (10.0, 15.0)]
    delete = [(5.0, 10.0)]
    expr = R.build_video_select_expr(keep, delete, fps, 20.0)
    assert expr, "应生成 select 表达式"
    # 采用保留式（保留段更少？此处相等，应取更短者）
    assert "between(t," in expr
    frames = R.expected_output_frames(keep, fps)
    assert frames == 5 * 30 + 5 * 30, frames
    print(f"[A1] 保留式 select 生成，帧数={frames}              OK")


def _eval_select(expr, fps, t):
    """在给定表达式下，判断时刻 t 的帧是否被保留（纯 Python 复刻语义）。"""
    if expr == "":
        return True
    if expr == "0":
        return False
    s = expr
    if s.startswith("not(") and s.endswith(")"):
        s = s[4:-1]
        return not _eval_select(s, fps, t)
    total = 0.0
    for part in s.split("+"):
        inner = part[len("between(t,"):-1]
        a, b = inner.split(",")
        if float(a) <= t <= float(b):
            total += 1
    return total > 0


def test_select_expr_delete_mode(cfg):
    fps = 25.0
    # 删除段更少时，函数自动选择更短的表达式（not(删除式) 或保留式）
    keep = [(0.0, 1.0), (9.0, 10.0)]
    delete = [(1.0, 3.0), (4.0, 6.0), (7.0, 9.0)]
    expr = R.build_video_select_expr(keep, delete, fps, 10.0)
    # 语义校验：保留区被保留、删除区被丢弃
    assert _eval_select(expr, fps, 0.5), expr      # 在 keep[0,1)
    assert _eval_select(expr, fps, 9.5), expr      # 在 keep[9,10)
    assert not _eval_select(expr, fps, 2.0), expr  # 在 delete[1,3)
    assert not _eval_select(expr, fps, 5.0), expr  # 在 delete[4,6)
    assert not _eval_select(expr, fps, 8.0), expr  # 在 delete[7,9)
    frames = R.expected_output_frames(keep, fps)
    assert frames == 1 * 25 + 1 * 25, frames
    print(f"[A2] 删除式选择更短表达式，帧数={frames}              OK")


def test_select_expr_full_keep(cfg):
    # 全片保留 -> 空表达式（不裁剪）
    expr = R.build_video_select_expr([(0.0, 20.0)], [], 30.0, 20.0)
    assert expr == "", repr(expr)
    print("[A3] 全片保留 -> 空 select（不裁剪）          OK")


def test_expected_frames_snap(cfg):
    fps = 30.0
    # 非整帧边界会被 snap（在分析阶段做），这里直接验证帧数公式
    keep = [(0.0, 2.0), (2.0, 10.0)]
    # [2.0, 10.0) 边界：round(2*30)=60, round(10*30)=300 -> 240 帧
    assert R.expected_output_frames(keep, fps) == 2 * 30 + (300 - 60)
    print("[A4] 帧数公式 = Σ(round(e*fps)-round(s*fps))    OK")


# ===========================================================================
# B. 采样级精确音频剪辑
# ===========================================================================
def test_audio_mute_and_beep(cfg, tmp):
    sr, ch = 48000, 2
    src = os.path.join(tmp, "src.wav")
    make_sine_wav(src, sr=sr, ch=ch, dur=10.0, freq=440.0)
    out = os.path.join(tmp, "out.wav")
    # 消音 [3,4)：原 440Hz 正弦 -> 零 + 1000Hz 哔声
    meta = audio_edit.render_audio(
        src, out, keep_ranges=[(0.0, 10.0)],
        mute_ranges=[(3.0, 4.0)],
        beep_freq=1000.0, beep_volume=0.3,
        lead_silence=0.0, expect_duration=None)
    assert meta["mute_applied"] == 1, meta
    buf, _, _ = read_wav_samples(out)
    # 原声 440Hz；消音段应是 1000Hz 哔声（频率不同），取消音中段采样看频率
    def est_freq(buf, ch, sr, center_sec, win=2000):
        i0 = int(center_sec * sr) * ch
        samples = [buf[i0 + k * ch] for k in range(win)]
        # 零交越计数估频
        zc = sum(1 for k in range(1, len(samples))
                 if (samples[k - 1] <= 0) != (samples[k] <= 0))
        return zc / 2.0 / (win / sr)
    f_mute = est_freq(buf, ch, sr, 3.5)
    f_keep = est_freq(buf, ch, sr, 1.5)
    assert abs(f_keep - 440) < 60, f_keep
    # 哔声 1000Hz（带淡入淡出，频率接近即可）
    assert abs(f_mute - 1000) < 150, f_mute
    print(f"[B1] 消音段被替换为哔声(≈{f_mute:.0f}Hz)        OK")


def test_audio_splice_duration_exact(cfg, tmp):
    """保留区间拼接后，音频时长必须与视频保留帧数严格相等。"""
    fps, sr, ch = 30.0, 48000, 2
    src = os.path.join(tmp, "src.wav")
    make_sine_wav(src, sr=sr, ch=ch, dur=20.0, freq=330.0)
    # 删除 [5,8) 和 [12,15) -> 视频保留帧数：
    keep = [(0.0, 5.0), (8.0, 12.0), (15.0, 20.0)]
    n_frames = R.expected_output_frames(keep, fps)   # 150+120+150 = 420
    body_dur = n_frames / fps                         # 14.0s
    out = os.path.join(tmp, "out2.wav")
    meta = audio_edit.render_audio(
        src, out, keep_ranges=keep, mute_ranges=(),
        beep_freq=1000.0, beep_volume=0.2,
        lead_silence=0.0, expect_duration=body_dur)
    # 对齐后采样数 = n_frames * sr / fps
    buf, _, got_sr = read_wav_samples(out)
    n_samples = len(buf) // ch
    expect_samples = int(round(body_dur * sr))
    assert abs(n_samples - expect_samples) <= 1, (n_samples, expect_samples)
    assert abs(meta["duration"] - body_dur) < 1e-4, meta["duration"]
    print(f"[B2] 拼接时长精确={meta['duration']:.6f}s "
          f"(={n_frames}帧/{fps}fps)            OK")


def test_audio_lead_silence_with_cover(cfg, tmp):
    """片头封面静音：lead_silence 前置且总时长对齐视频。"""
    fps, sr, ch = 30.0, 48000, 2
    src = os.path.join(tmp, "src.wav")
    make_sine_wav(src, sr=sr, ch=ch, dur=10.0, freq=330.0)
    keep = [(0.0, 10.0)]
    n_frames = R.expected_output_frames(keep, fps)    # 300
    cover_s = 3.0
    body_dur = n_frames / fps                          # 10.0
    out = os.path.join(tmp, "out3.wav")
    meta = audio_edit.render_audio(
        src, out, keep_ranges=keep, mute_ranges=(),
        beep_freq=1000.0, beep_volume=0.2,
        lead_silence=cover_s, expect_duration=cover_s + body_dur)
    buf, _, _ = read_wav_samples(out)
    n_samples = len(buf) // ch
    # 前 cover_s 秒应为静音（全 0），其后才是内容
    lead_samples = int(round(cover_s * sr)) * ch
    lead_rms = (sum(buf[k] * buf[k] for k in range(0, lead_samples, ch)) / (lead_samples // ch)) ** 0.5
    assert lead_rms < 1.0, lead_rms
    assert abs(n_samples - int(round((cover_s + body_dur) * sr))) <= 1
    print(f"[B3] 片头静音 {cover_s}s + 正文 {body_dur}s 对齐     OK")


# ===========================================================================
# C. 全流程 render（合成视频，需 ffmpeg）
# ===========================================================================
def _make_synthetic_video(path, dur=20.0, fps=30, w=320, h=240):
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
        path,
    ])
    return path


def test_render_end_to_end(cfg, tmp):
    if not ffmpeg_available():
        print("[C] 跳过：环境无 ffmpeg")
        return
    src = os.path.join(tmp, "src.mp4")
    _make_synthetic_video(src, dur=20.0, fps=30, w=320, h=240)
    info = probe_video(src)
    fps = 30.0

    plan = EditPlan(source=src, duration=info["duration"], fps=fps,
                    width=info["width"], height=info["height"])
    # 删除 [5,8) 议价；删除 [12,15) 高风险画面；消音 [3,4) 敏感数据
    plan.add_delete(5.0, 8.0, T.T_NEGOTIATION, "讨论价格")
    plan.add_delete(12.0, 15.0, T.T_HIGH_RISK, "微信界面")
    plan.add_mute(3.0, 4.0, "提到营业额")
    plan.subtitle_cues = [
        {"start": 1.0, "end": 2.0, "text": "正常演示内容"},
        {"start": 6.0, "end": 7.0, "text": "被删除的议价字幕"},
    ]
    plan.cover = {"disabled": True}     # 关闭封面，使时长确定、无需 PIL
    plan.normalize()

    keep = plan.keep_ranges(snap=True)
    n_frames = R.expected_output_frames(keep, fps)
    body_dur = n_frames / fps
    # 预期删除：5~8(3s)+12~15(3s)=6s；保留 20-6=14s -> 420 帧
    assert n_frames == 420, n_frames

    out = os.path.join(tmp, "final.mp4")
    cfg = Config(workdir=tmp, burn_subtitle=True, cover_duration=0.0)
    res = R.render(plan, cfg, out, video=src)

    # 成片校验
    oinfo = probe_video(out)
    assert oinfo["width"] == info["width"]
    assert oinfo["height"] == info["height"]
    assert abs(oinfo["fps"] - fps) < 0.05
    assert oinfo["has_audio"], "成片应保留音轨"
    # 时长与预期一致（覆盖 _verify 自身的 0.5s 容差）
    assert abs(oinfo["duration"] - body_dur) < 0.5, (oinfo["duration"], body_dur)

    # 音画同步核心：成片音频采样数 == 成片视频帧数 × sr / fps
    awav = os.path.join(tmp, "render_audio.wav")
    assert os.path.exists(awav), "render 未产出剪辑后音频"
    abuf, ach, asr = read_wav_samples(awav)
    a_samples = len(abuf) // ach
    # 无封面时 expect_duration = body_dur
    expect_a = int(round(body_dur * asr))
    assert abs(a_samples - expect_a) <= 2, (a_samples, expect_a)

    # 帧级精确：成片实际解码帧数 == 预期
    cnt = run([
        "ffprobe", "-v", "error", "-count_frames",
        "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
        "-of", "default=nokey=1:noprint_wrappers=1", out,
    ])
    got_frames = int(cnt.stdout.strip().splitlines()[-1])
    assert abs(got_frames - n_frames) <= 2, (got_frames, n_frames)

    # 外挂 SRT 已重算：被删除区间的字幕不应出现在成片
    srt = os.path.splitext(out)[0] + ".srt"
    assert os.path.exists(srt)
    with open(srt, encoding="utf-8") as f:
        srt_txt = f.read()
    assert "正常演示内容" in srt_txt
    assert "被删除的议价字幕" not in srt_txt, "议价字幕泄漏到成片"

    print(f"[C1] 一次编码完成：{oinfo['width']}x{oinfo['height']} "
          f"@ {oinfo['fps']:.2f}fps {oinfo['duration']:.2f}s "
          f"({n_frames}帧)                 OK")
    print(f"[C2] 音画同步：音频{a_samples}采样 == 视频{n_frames}帧×sr/fps "
          f"({expect_a})            OK")
    print(f"[C3] 帧级精确删除：解码 {got_frames} 帧 == 预期 {n_frames}    OK")
    print(f"[C4] 外挂 SRT 已重算、无泄漏议价字幕          OK")


def test_render_with_cover(cfg, tmp):
    if not ffmpeg_available():
        print("[C-cover] 跳过：环境无 ffmpeg")
        return
    src = os.path.join(tmp, "src2.mp4")
    _make_synthetic_video(src, dur=10.0, fps=30, w=320, h=240)
    info = probe_video(src)
    fps = 30.0
    plan = EditPlan(source=src, duration=info["duration"], fps=fps,
                    width=info["width"], height=info["height"])
    plan.cover = {"title": "库存管理演示", "frame_ts": 0.0}
    plan.normalize()
    keep = plan.keep_ranges(snap=True)
    n_frames = R.expected_output_frames(keep, fps)
    body_dur = n_frames / fps

    out = os.path.join(tmp, "final_cover.mp4")
    cfg = Config(workdir=tmp, burn_subtitle=False, cover_duration=3.0)
    res = R.render(plan, cfg, out, video=src)
    oinfo = probe_video(out)
    cover_s = R._cover_seconds(plan, cfg, fps)
    expect = cover_s + body_dur
    assert abs(oinfo["duration"] - expect) < 0.5, (oinfo["duration"], expect)
    print(f"[C5] 封面片头 {cover_s:.2f}s 已拼入，总时长 "
          f"{oinfo['duration']:.2f}s == {expect:.2f}s        OK")


if __name__ == "__main__":
    cfg = Config(workdir="./_t4work", pause_mode="trim")
    tmp = tempfile.mkdtemp(prefix="t4_")
    try:
        test_select_expr_basic(cfg)
        test_select_expr_delete_mode(cfg)
        test_select_expr_full_keep(cfg)
        test_expected_frames_snap(cfg)
        test_audio_mute_and_beep(cfg, tmp)
        test_audio_splice_duration_exact(cfg, tmp)
        test_audio_lead_silence_with_cover(cfg, tmp)
        test_render_end_to_end(cfg, tmp)
        test_render_with_cover(cfg, tmp)
        print("\nStep 4 一次 FFmpeg filter_complex 最终处理：全部通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
