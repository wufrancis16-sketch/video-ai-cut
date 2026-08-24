"""Step 8 补充：分窗渲染路径（短源 + 密集切点，低成本验证分窗逻辑）。

主 Step 8（test_long_video.py）用 40 分钟源 + 80 切点验证**长窗**形态。
本测试刻意用 60 秒短源 + 150 个密集切点，使内联单 select 滤镜图远超
SAFE_INLINE_GRAPH，强制触发「分窗单命令」路径，验证**密集切点**形态下仍能正确
渲染（每窗一个独立 -ss/-to 输入，只解码自身时间窗，既不触发单条 select 的解析
OOM，也不触发多分支读同一输入的并行解码 OOM）。
"""
from __future__ import annotations

from array import array
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import render, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.utils import run, probe_video, ffmpeg_available  # noqa: E402


def _synth_source(work, dur=60.0, fps=30, W=1280, H=720, sr=48000):
    video = os.path.join(work, "chunk_src_video.mp4")
    out = os.path.join(work, "chunk_source.mp4")
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc=size={W}x{H}:rate={fps}:duration={dur}",
         "-f", "lavfi", "-i", f"aevalsrc=0:d={dur}",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-c:a", "aac", "-pix_fmt", "yuv420p", out])
    return out


def _build_dense_plan(path, dur=60.0):
    # 每 0.4s 一个 0.2s 删除段 -> 150 个删除 / 约 150 个保留间隙
    plan = T.EditPlan(source=path, duration=dur, fps=30.0,
                      width=1280, height=720, subtitle=False)
    n = 150
    for i in range(n):
        s = 0.1 + i * 0.4
        e = s + 0.2
        if e > dur - 0.1:
            break
        plan.add_delete(s, e, T.T_NEGOTIATION, "分桶压测删除段")
    plan.normalize()
    return plan


def main():
    assert ffmpeg_available(), "需要 ffmpeg"
    work = os.path.join(ROOT, "_step8chunk_work")
    os.makedirs(work, exist_ok=True)
    for f in os.listdir(work):
        try:
            os.remove(os.path.join(work, f))
        except OSError:
            pass

    print("[Step8b] 生成 60 秒合成源视频 ...")
    src = _synth_source(work)
    info = probe_video(src)
    print(f"[源] {info['width']}x{info['height']} @ {info['fps']:.3f}fps "
          f"{info['duration']:.0f}s audio={info['has_audio']}")

    cfg = Config.load(use_llm=False, trim_intro=False,
                      detect_bargaining=False, detect_sensitive_screen=False,
                      remove_filler=False, pause_mode="off",
                      cover_duration=0.0, burn_subtitle=False,
                      final_preset="ultrafast", final_crf=23,
                      workdir=work)

    plan = _build_dense_plan(src, info["duration"])
    print(f"[方案] 删除 {len(plan.delete_segments)} 段 / "
          f"保留 {len(plan.keep_ranges())} 段")

    keep = plan.keep_ranges()
    # LP6：内联滤镜图必然超长 -> 触发分窗 decode-once 路径
    glen = render._inline_graph_len(
        keep, plan.delete_ranges(), info["fps"], info["duration"],
        has_audio=info["has_audio"])
    print(f"[LP6] 内联单 select 滤镜图长度 = {glen} 字符（安全上限 "
          f"{render.SAFE_INLINE_GRAPH}）-> 应触发分窗 decode-once 路径")
    assert glen > render.SAFE_INLINE_GRAPH, "设计前提：本用例应触发分窗路径"

    out = os.path.join(work, "chunk_final.mp4")
    r = render.render(plan, cfg, out, src)
    i = probe_video(out)

    # LP2 分辨率/帧率不变
    assert i["width"] == info["width"] and i["height"] == info["height"]
    assert abs(i["fps"] - info["fps"]) < 0.1
    print(f"[LP2] 文字清晰：输出 {i['width']}x{i['height']} @ "
          f"{i['fps']:.3f}fps   OK")

    # LP5 时长完整
    assert abs(i["duration"] - r["expected_duration"]) < 0.5, \
        f"成片时长异常：{i['duration']:.3f} vs {r['expected_duration']:.3f}"
    print(f"[LP5] 末尾完整：成片 {i['duration']:.3f}s / 预期 "
          f"{r['expected_duration']:.3f}s   OK")

    # LP3 音画同步（静音源，仅校验采样数与帧数成比例）
    buf, sr1 = _extract_audio(out, i["sample_rate"] or 48000)
    samples = len(buf)
    expect = int(round(r["frames"] * sr1 / i["fps"]))
    assert abs(samples - expect) <= sr1 / i["fps"] * 1.5 + 5, \
        f"音画不同步：采样 {samples} vs 期望 {expect}"
    print(f"[LP3] 音画同步：音频 {samples} 采样 == 视频 {r['frames']} 帧 "
          f"× {sr1}/{i['fps']:.1f} = {expect}   OK")

    # LP1 渲染成功（分窗路径未抛异常即代表成功）
    print(f"[LP1] 分窗渲染成功（极端 {len(plan.delete_segments)} 切点，"
          f"无解析 OOM / 无并行解码 OOM）   OK")

    print(f"\nStep 8b 分窗路径通过：源 {info['duration']:.0f}s -> "
          f"成片 {i['duration']:.1f}s，删除 {len(plan.delete_segments)} 段")


def _extract_audio(path, sr=48000):
    wav = path + ".audio.wav"
    run(["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "pcm_s16le",
         "-ar", str(sr), "-ac", "1", wav])
    with wave.open(wav, "rb") as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
    b = array("h")
    b.frombytes(raw)
    return b, wf.getframerate()


if __name__ == "__main__":
    main()
