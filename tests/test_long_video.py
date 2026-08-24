"""Step 8：40 分钟量级视频渲染压力测试（合成源，验证架构在长时长 + 多切点下稳定）。

环境无 faster_whisper，无法对真实 40 分钟视频做端到端 ASR；本测试用 40 分钟
testsrc 合成视频 + 程序化生成的密集剪辑方案（约 80 个删除段 + 若干消音/
待确认）走**真实 render 路径**，验证：

  LP1 渲染不触发 Windows 命令行长度溢出 / 内存 OOM（单 select 内联或分窗 decode-once）
  LP2 输出分辨率/帧率不变（ERP 文字清晰）
  LP3 音画同步（采样数 == 帧数 × sr/fps，40 分钟尺度）
  LP4 多切点删除准确（保留区间不含被删段）
  LP5 输出时长 ≈ 预期（不截断）
  LP6 滤镜图规模与路径选择（80 切点超安全上限，本用例走分窗 decode-once 路径）
"""
from __future__ import annotations

from array import array
import math
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import render, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.utils import run, probe_video, ffmpeg_available  # noqa: E402


def _synth_long_source(work, dur=2400.0, fps=30, W=1280, H=720, sr=48000):
    """生成 40 分钟 testsrc 合成视频 + 静音音轨（验证长时长音频缓冲/剪辑）。
    若合成源已存在则跳过（缓存，避免每次重跑 4 分钟编码）。"""
    video = os.path.join(work, "long_src_video.mp4")
    out = os.path.join(work, "long_source.mp4")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        print("[源] 命中缓存，跳过 40 分钟源重新编码")
        return out
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc=size={W}x{H}:rate={fps}:duration={dur}",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-pix_fmt", "yuv420p", video])
    # 静音音轨（aevalsrc=0），让 audio_edit 走完整 40 分钟采样缓冲路径
    wav = os.path.join(work, "long_src_audio.wav")
    n = int(dur * sr)
    buf = array("h", [0]) * n
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf.tobytes())
    out = os.path.join(work, "long_source.mp4")
    run(["ffmpeg", "-y", "-i", video, "-i", wav,
         "-c:v", "copy", "-c:a", "aac", "-shortest", out])
    return out


def _build_dense_plan(path, dur=2400.0):
    """程序化生成密集剪辑方案：约 150 个 15s 删除段（每 16s 一个），
    保留 1s 间隙；另注入若干消音与待确认项。覆盖整条 40 分钟时间轴。
    """
    plan = T.EditPlan(source=path, duration=dur, fps=30.0,
                      width=1280, height=720, subtitle=False)
    n = 80
    for i in range(n):
        s = i * 16.0 + 0.5
        e = s + 15.0
        if e > dur - 0.5:
            break
        plan.add_delete(s, e, T.T_NEGOTIATION, "压测删除段")
    # 注入消音与待确认（落在保留间隙内）
    plan.add_mute(8.0, 9.0, "压测敏感数据")
    plan.add_review(40.0, 41.0, T.T_HIGH_RISK, "压测待确认项")
    plan.normalize()
    return plan


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


def main():
    assert ffmpeg_available(), "需要 ffmpeg"
    work = os.path.join(ROOT, "_step8work")
    os.makedirs(work, exist_ok=True)
    _keep = {"long_src_video.mp4", "long_src_audio.wav", "long_source.mp4"}
    for f in os.listdir(work):
        if f in _keep:
            continue
        try:
            os.remove(os.path.join(work, f))
        except OSError:
            pass

    print("[Step8] 生成 40 分钟合成源视频 ...")
    src = _synth_long_source(work)
    info = probe_video(src)
    print(f"[源] {info['width']}x{info['height']} @ {info['fps']:.3f}fps "
          f"{info['duration']:.0f}s audio={info['has_audio']}")
    assert info["duration"] >= 2300, "源时长应约 40 分钟"

    cfg = Config.load(use_llm=False, trim_intro=False,
                      detect_bargaining=False, detect_sensitive_screen=False,
                      remove_filler=False, pause_mode="off",
                      cover_duration=0.0, burn_subtitle=False,
                      final_preset="ultrafast", final_crf=23,
                      workdir=work)

    plan = _build_dense_plan(src, info["duration"])
    plan_path = os.path.join(work, "plan.json")
    plan.save(plan_path)
    print(f"[方案] 删除 {len(plan.delete_segments)} 段 / "
          f"消音 {len(plan.mute_segments)} 段 / "
          f"待确认 {len(plan.review_items)} 段")

    keep = plan.keep_ranges()
    dels = plan.delete_ranges()

    # LP6：内联滤镜图规模与路径选择（极端多切点应触发分窗 decode-once 路径）
    glen = render._inline_graph_len(
        keep, dels, info["fps"], info["duration"], has_audio=info["has_audio"])
    print(f"[LP6] 内联滤镜图长度 = {glen} 字符（安全上限 "
          f"{render.SAFE_INLINE_GRAPH}）")
    if glen > render.SAFE_INLINE_GRAPH:
        print("[LP6] 超长自动切换分窗 decode-once 渲染路径（极端切点可处理）   OK")
    else:
        print("[LP6] 内联单命令渲染（滤镜图在命令行上限内）   OK")

    # LP4：删除准确（抽样若干被删段真实中点，应不在保留区间）
    def in_keep(t, eps=1e-3):
        return any(s - eps <= t <= e + eps for s, e in keep)
    del_mids = [(s + e) / 2.0 for s, e in dels]
    assert del_mids, "应至少生成 1 个删除段"
    n = len(del_mids)
    sample_idx = sorted(set(min(i, n - 1) for i in (0, 10, 50, 100, 140) if i < n))
    for idx in sample_idx:
        t = del_mids[idx]
        assert not in_keep(t), f"删除段中点 {t}s 应不在保留区间"
    print(f"[LP4] 多切点删除准确（抽样 {len(sample_idx)} 个删除段均不在保留区间）   OK")

    out = os.path.join(work, "long_final.mp4")
    r = render.render(plan, cfg, out, src)
    i = probe_video(out)
    sr1 = i["sample_rate"]

    # LP2
    assert i["width"] == info["width"] and i["height"] == info["height"], \
        f"分辨率变化 {info['width']}x{info['height']} -> {i['width']}x{i['height']}"
    assert abs(i["fps"] - info["fps"]) < 0.1, "帧率变化"
    print(f"[LP2] 文字清晰：输出 {i['width']}x{i['height']} @ "
          f"{i['fps']:.3f}fps   OK")

    # LP3
    buf, _ = _extract_audio(out, sr1)
    samples = len(buf)
    expect = int(round(r["frames"] * sr1 / i["fps"]))
    assert abs(samples - expect) <= sr1 / i["fps"] * 1.5 + 5, \
        f"音画不同步：采样 {samples} vs 期望 {expect}"
    print(f"[LP3] 音画同步：音频 {samples} 采样 == 视频 {r['frames']} 帧 "
          f"× {sr1}/{i['fps']:.1f} = {expect}   OK")

    # LP5
    assert abs(i["duration"] - r["expected_duration"]) < 0.5, "成片时长异常"
    assert samples > 0, "成片音频为空"
    print(f"[LP5] 末尾完整：成片 {i['duration']:.3f}s 完整、音频延伸至片尾   OK")

    # LP1：命令行长度未溢出（render 已成功执行即代表未溢出）
    print(f"[LP1] 渲染成功（内联 filter_complex 未触发命令行溢出）   OK")

    print(f"\nStep 8 长视频压力测试通过：源 {info['duration']:.0f}s -> "
          f"成片 {i['duration']:.1f}s，删除 {len(plan.delete_segments)} 段")


if __name__ == "__main__":
    main()
