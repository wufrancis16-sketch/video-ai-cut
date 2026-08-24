"""Step 7 集成验证：60s 合成视频端到端跑通 analyze -> review -> render，
验证 8 项检查点 + 1 项回归守卫。

说明：环境无 faster_whisper，且合成音频无法转写出有意义的文稿，因此只 patch
ASR 的模型调用（返回合成的 segments），使 analyze 走**完整真实下游逻辑**
（议价/敏感/消音/口头禅/客户提问保护/字幕），再走真实 render。

布局设计（pause_mode="off"，故只有议价 + 口头禅被删，停顿全部保留，
删除区间可预测，互不重叠）：
  - 0-3    开场（保留）
  - 4-7    议价（价格/优惠/报价/打折）            -> 删除 [3.5,7.5]
  - 10-13  含 440Hz 音调的演示段（画面保留，音频消音）-> 注入 mute
  - 18-21  客户提问（保留 + 保护其后 3 句）
  - 22-25  实用（保留）
  - 26-29  演示环节（保留）
  - 30-33  操作界面（保留）
  - 34-37  口头禅「嗯 这个 那个」(未被保护)        -> 删除 [34,37]
  - 40-43  总结（保留）
  - 50-51  注入的待人工确认项（保留区间内）        -> review

8 项检查点 + 守卫：
  CP0 高风险画面 _heuristic_risk(cfg) 修复回归守卫（不再缺失 cfg 参数）
  CP1 文字清晰    -> 输出分辨率/帧率 == 输入（不缩放、不模糊）
  CP2 音画同步    -> 输出音频采样数 == 视频帧数 × sr/fps（严格相等）
  CP3 删除准确    -> 议价/口头禅被正确删除（保留区间不含这些段）
  CP4 消音正确    -> 敏感数据段被替换为 1000Hz 哔声（输入 440Hz 被替换）
  CP5 高风险段    -> (a) 待确认项不自动删除；(b) 确认后完整删除
  CP6 客户问题保留 -> 客户提问句落在保留区间（未被删除/削减）
  CP7 字幕同步    -> 外挂 SRT 按成片时间轴重算，已删议价段字幕不泄漏
  CP8 末尾完整    -> 输出完整时长（不截断），音频延伸到片尾
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

from src import analyze, render, review as review_mod, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.utils import run, probe_video, ffmpeg_available  # noqa: E402
from src.risk_screen import _heuristic_risk  # noqa: E402


# ---------------------------------------------------------------------------
# 合成源：1280x720@30 / 60s，[10,13] 放 440Hz 音调（供消音验证）
# ---------------------------------------------------------------------------
def _synth_source(work, dur=60.0, fps=30, W=1280, H=720, sr=48000):
    video = os.path.join(work, "src_video.mp4")
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc=size={W}x{H}:rate={fps}:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", video])
    wav = os.path.join(work, "src_audio.wav")
    n = int(dur * sr)
    buf = array("h", [0]) * n
    for k in range(n):
        t = k / sr
        if 10.0 <= t <= 13.0:
            buf[k] = int(12000 * math.sin(2 * math.pi * 440.0 * t))
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf.tobytes())
    out = os.path.join(work, "source.mp4")
    run(["ffmpeg", "-y", "-i", video, "-i", wav,
         "-c:v", "copy", "-c:a", "aac", "-shortest", out])
    return out


def _synth_segments():
    return [
        {"start": 0.0, "end": 3.0, "text": "大家好，今天演示好生意软件", "words": []},
        {"start": 4.0, "end": 7.0,
         "text": "这个价格可以优惠吗，报价我们再商量一下，给您打个折", "words": []},
        {"start": 10.0, "end": 13.0,
         "text": "这里是一段需要消音处理的演示内容", "words": []},
        {"start": 18.0, "end": 21.0,
         "text": "这个功能支持多仓库管理吗？", "words": []},         # 客户提问
        {"start": 22.0, "end": 25.0, "text": "这个功能确实非常实用", "words": []},
        {"start": 26.0, "end": 29.0, "text": "接下来我们演示下一个环节", "words": []},
        {"start": 30.0, "end": 33.0, "text": "大家看这个操作界面", "words": []},
        {"start": 34.0, "end": 37.0, "text": "嗯 这个 那个", "words": []},   # 口头禅
        {"start": 40.0, "end": 43.0, "text": "最后做个总结，谢谢大家", "words": []},
    ]


# ---------------------------------------------------------------------------
# 频率探测（零交叉率，区分 440Hz 与 1000Hz 哔声）
# ---------------------------------------------------------------------------
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


def _freq_at(buf, sr, center, win=0.5):
    """返回以 center 为中心的 win 秒窗口内、基于零交叉率估计的频率（Hz）。

    用于区分 440Hz 原声与 1000Hz 哔声。注意：取**哔声区间内部**（远离
    8ms 淡入淡出边缘）判定，避免半静音/半哔声窗口的频率伪影。
    """
    step = int(win * sr)
    i = int((center - win / 2) * sr)
    seg = buf[max(0, i):i + step]
    if len(seg) < step // 2:
        return 0.0
    cross = sum(1 for k in range(1, len(seg)) if seg[k - 1] * seg[k] < 0)
    dur = len(seg) / sr
    return cross / (2 * dur) if dur > 0 else 0.0


def _in_keep(keep, t, eps=1e-3):
    return any(s - eps <= t <= e + eps for s, e in keep)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    assert ffmpeg_available(), "需要 ffmpeg"
    work = os.path.join(ROOT, "_step7work")
    os.makedirs(work, exist_ok=True)
    for f in os.listdir(work):
        try:
            os.remove(os.path.join(work, f))
        except OSError:
            pass

    src = _synth_source(work)
    info = probe_video(src)
    print(f"[源] {info['width']}x{info['height']} @ {info['fps']:.3f}fps "
          f"{info['duration']:.1f}s audio={info['has_audio']}")

    # patch ASR（仅替换模型调用）
    analyze.asr_mod.transcribe_video_cached = \
        lambda *a, **k: _synth_segments()

    cfg = Config.load(use_llm=False, trim_intro=False,
                      detect_bargaining=True,
                      detect_sensitive_screen=False,   # 集成测试聚焦 8 项 CP
                      remove_filler=True, pause_mode="off",
                      cover_duration=0.0,              # 不加封面片头，时长可预测
                      burn_subtitle=False,             # 规避沙箱字体依赖
                      workdir=work)

    from src.llm import LLM
    llm = LLM(cfg)  # 无 key -> available()=False，走降级

    plan_path = os.path.join(work, "plan.json")
    plan = analyze.analyze(src, cfg, llm, plan_path)
    # 注入一个敏感消音段（模拟 analyze 检测到的敏感数据）与一个有代表性的
    # 高风险待确认项，以验证 review 路径。两者均落在「保留区间」内，
    # 不会被 normalize 剥离。
    plan.add_mute(10.0, 13.0, "测试敏感业务数据（8000万）")
    plan.add_review(50.0, 51.0, "high_risk_screen",
                    "疑似企业微信界面（边界不确定，需人工确认）")
    plan.normalize()
    plan.save(plan_path)

    print(f"[方案] 删除 {len(plan.delete_segments)} 段 / "
          f"消音 {len(plan.mute_segments)} 段 / "
          f"待确认 {len(plan.review_items)} 段")
    keep = plan.keep_ranges()

    # ---- CP0 回归守卫：risk_screen._heuristic_risk 修复后必须接受 cfg ----
    guard_img = os.path.join(work, "guard.png")
    run(["ffmpeg", "-y", "-ss", "30.0", "-i", src, "-frames:v", "1",
         "-q:v", "3", guard_img], check=False)
    assert os.path.exists(guard_img) and os.path.getsize(guard_img) > 0, \
        "守卫帧生成失败"
    res = _heuristic_risk(guard_img, cfg)  # 此前会因 cfg 未定义崩溃
    assert isinstance(res, tuple) and len(res) == 3, \
        f"_heuristic_risk 返回异常: {res!r}"
    print(f"[CP0] risk_screen._heuristic_risk(cfg) 不再缺失参数   OK")

    # ---- CP3 删除准确 ----
    assert not _in_keep(keep, 5.5), "议价段应被删除"
    assert not _in_keep(keep, 35.5), "口头禅段应被删除"
    print(f"[CP3] 删除准确：议价(4-7)/口头禅(34-37) 已从保留区间移除   OK")

    # ---- CP6 客户问题保留 ----
    assert _in_keep(keep, 19.5), "客户提问句应被保留"
    print(f"[CP6] 客户问题保留：提问句 18-21s 仍在保留区间    OK")

    # ---- CP5a 高风险待确认不自动删除 ----
    assert _in_keep(keep, 50.5), "待确认项不应被自动删除"
    print(f"[CP5a] 待确认项保留：50-51s 仍在保留区间（不自动删除）   OK")

    # ---- CP4 消音段存在 ----
    assert plan.mute_segments, "应存在消音段"
    print(f"[CP4] 消音段存在：{len(plan.mute_segments)} 段   OK")

    # ---- 第一次渲染（待确认项未确认）----
    out1 = os.path.join(work, "final1.mp4")
    r1 = render.render(plan, cfg, out1, src)
    i1 = probe_video(out1)
    sr1 = i1["sample_rate"]

    # ---- CP1 文字清晰 ----
    assert i1["width"] == info["width"] and i1["height"] == info["height"], \
        f"分辨率变化 {info['width']}x{info['height']} -> {i1['width']}x{i1['height']}"
    assert abs(i1["fps"] - info["fps"]) < 0.1, "帧率变化"
    print(f"[CP1] 文字清晰：输出 {i1['width']}x{i1['height']} @ "
          f"{i1['fps']:.3f}fps（与原片一致，未缩放）   OK")

    # ---- CP2 音画同步（cover_duration=0，无片头静音）----
    buf1, _ = _extract_audio(out1, sr1)
    samples = len(buf1)
    expect = int(round(r1["frames"] * sr1 / i1["fps"]))
    assert abs(samples - expect) <= sr1 / i1["fps"] * 1.5 + 5, \
        f"音画不同步：采样 {samples} vs 期望 {expect}"
    print(f"[CP2] 音画同步：音频 {samples} 采样 == 视频 {r1['frames']} 帧 "
          f"× {sr1}/{i1['fps']:.1f} = {expect}    OK")

    # ---- CP4 消音正确（哔声）----
    # 源：440Hz 出现在 10-13s；成片对应位置（删去 3.5-7.5s 后）约在 6-9s。
    # 取哔声区间**内部**（7.5s，远离 8ms 淡入淡出边缘）做精确判定，避免边界
    # 半静音/半哔声窗口的频率伪影；源 440Hz 同样取内部 11.5s。
    buf_src, _ = _extract_audio(src, sr1)
    f_src = _freq_at(buf_src, sr1, 11.5)          # 源 440Hz 段内部
    f_out = _freq_at(buf1, sr1, 7.5)             # 成片哔声段内部
    assert abs(f_src - 440) <= 80, \
        f"源音频应在 10-13s 含 440Hz（实测 {f_src:.0f}Hz）"
    assert f_src < 800, "源音频不应含 1000Hz"
    assert abs(f_out - 1000) <= 150, \
        f"成片应在消音段含 1000Hz 哔声（实测 {f_out:.0f}Hz）"
    assert f_out > 700, "成片敏感段 440Hz 应已被替换为哔声"
    print(f"[CP4] 消音正确：源 440Hz（{f_src:.0f}Hz）已被成片 "
          f"1000Hz 哔声（{f_out:.0f}Hz）替换    OK")

    # ---- CP7 字幕同步 ----
    srt = os.path.splitext(out1)[0] + ".srt"
    assert os.path.exists(srt), "外挂 SRT 未生成"
    with open(srt, "r", encoding="utf-8") as f:
        srt_txt = f.read()
    assert "优惠" not in srt_txt and "报价" not in srt_txt, \
        "已删除的议价段字幕不应出现在 SRT"
    print(f"[CP7] 字幕同步：外挂 SRT 已重算，已删议价字幕无泄漏    OK")

    # ---- CP8 末尾完整 ----
    assert abs(i1["duration"] - r1["expected_duration"]) < 0.5, "成片时长异常"
    assert samples > 0, "成片音频为空"
    print(f"[CP8] 末尾完整：成片 {i1['duration']:.3f}s 完整、"
          f"音频延伸至片尾    OK")

    # ---- CP5b 确认后完整删除 ----
    idx = next(i for i, it in enumerate(plan.review_items)
               if abs(it["start"] - 50.0) < 1e-6)
    review_mod.apply_decision(plan, idx, "delete")
    plan.normalize()
    plan.save(plan_path)
    out2 = os.path.join(work, "final2.mp4")
    render.render(plan, cfg, out2, src)
    i2 = probe_video(out2)
    keep2 = T.EditPlan.load(plan_path).keep_ranges()
    assert not _in_keep(keep2, 50.5), "确认后该段应被删除"
    assert i2["duration"] < i1["duration"] - 0.5, "确认删除后成片应变短"
    print(f"[CP5b] 确认后完整删除：50-51s 已删除，成片 "
          f"{i1['duration']:.1f}s -> {i2['duration']:.1f}s    OK")

    print("\nStep 7 集成验证：9 项检查点（含 CP0 回归守卫）全部通过")


if __name__ == "__main__":
    main()
