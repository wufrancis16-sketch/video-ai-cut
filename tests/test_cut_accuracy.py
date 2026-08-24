"""剪辑准确性：像素级验证「成片每一帧都来自正确的源帧」。

为什么必须有这个测试
--------------------
时长校验、采样数校验都**无法**发现「整体帧偏移」：只要帧数对，时长和音画同步
就都能对上，但画面可能整体错位——这意味着本该删掉的敏感画面仍留在成片里。
按需求优先级（隐私安全 > 剪辑准确性 > 音画同步 > 画质 > 速度），这是最高优先级
的正确性属性，必须逐帧验证。

方法
----
1. 合成一段「每一帧亮度都可区分」的源视频：
   brightness = mod(n,16)*0.015 + n*0.0004
   - 细粒度项：相邻帧亮度差约 3.8（±1 帧偏移立刻可见）
   - 慢速斜坡项：每帧 +0.1，16 帧累计 +1.6（打破 mod 16 的混叠，
     任意大小的偏移都会改变亮度）
2. 用 ffprobe signalstats 逐帧读出源与成片的 YAVG。
3. 按 keep 区间展开出「期望源帧号序列」，逐帧比对
   out_YAVG[k] ≈ src_YAVG[expected_frame[k]]。

同时覆盖 render.py 的两条路径（见 SKILL.md Hard Rule 9）：
  - CASE A 少量切点  -> 内联单 select 路径
  - CASE B 大量切点  -> 分窗单命令路径（每窗独立 -ss/-to 输入）
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import render, timeline as T  # noqa: E402
from src.config import Config  # noqa: E402
from src.utils import run, probe_video, ffmpeg_available  # noqa: E402

DUR = 40.0
FPS = 30
W, H = 320, 240
# YAVG 容差。源为纯色帧 -> YAVG 是整数；成片走无损编码（final_crf=0）且滤镜链
# 不改变像素值，因此偏差应为 0。实测相邻帧最小亮度阶梯为 2（整数量化后
# 2/5/3 交替），取 0.6 既能容忍舍入又远小于阶梯，保证 ±1 帧偏移必被发现。
TOL = 0.6


def _synth_ramp_source(work: str) -> str:
    """每帧亮度唯一可辨的合成源（无损编码，保证 YAVG 可精确比对）。"""
    out = os.path.join(work, "ramp_src.mp4")
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    run(["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={DUR}",
         "-f", "lavfi", "-i", f"aevalsrc=0:d={DUR}",
         "-vf", r"eq=brightness=mod(n\,16)*0.015+n*0.0004:eval=frame,"
                "format=yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
         "-c:a", "aac", "-shortest", out])
    return out


def _frame_luma(path: str):
    """逐帧 YAVG 列表。用 movie= 需规避 Windows 盘符冒号 -> chdir + 纯文件名。"""
    work = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    prev = os.getcwd()
    os.chdir(work)
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-f", "lavfi",
             "-i", f"movie={name},signalstats",
             "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
             "-of", "csv=p=0"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace")
    finally:
        os.chdir(prev)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe signalstats 失败: {p.stderr[-400:]}")
    vals = []
    for line in p.stdout.splitlines():
        line = line.strip().rstrip(",")
        if line:
            vals.append(float(line))
    return vals


def _expected_source_frames(keep, fps):
    """按 keep 区间展开成片每一帧对应的源帧号。"""
    seq = []
    for s, e in keep:
        seq.extend(range(round(s * fps), round(e * fps)))
    return seq


def _case(name, src, work, src_y, deletes, expect_windowed):
    cfg = Config.load(use_llm=False, trim_intro=False,
                      detect_bargaining=False, detect_sensitive_screen=False,
                      remove_filler=False, pause_mode="off",
                      cover_duration=0.0, burn_subtitle=False,
                      final_preset="ultrafast", final_crf=0,
                      workdir=work)
    plan = T.EditPlan(source=src, duration=DUR, fps=float(FPS),
                      width=W, height=H, subtitle=False)
    for s, e in deletes:
        plan.add_delete(s, e, T.T_NEGOTIATION, "准确性测试删除段")
    plan.normalize()
    keep = plan.keep_ranges(snap=True)

    glen = render._inline_graph_len(
        keep, plan.delete_ranges(snap=True), float(FPS), DUR, has_audio=True)
    windowed = glen > render.SAFE_INLINE_GRAPH
    assert windowed == expect_windowed, (
        f"{name}: 期望{'分窗' if expect_windowed else '内联'}路径，"
        f"实际滤镜图 {glen} 字符 vs 阈值 {render.SAFE_INLINE_GRAPH}")
    path_name = "分窗单命令" if windowed else "内联单 select"
    print(f"\n=== {name}：{len(deletes)} 个删除段 -> {path_name}路径"
          f"（滤镜图 {glen} 字符）===")

    out = os.path.join(work, f"acc_{name}.mp4")
    r = render.render(plan, cfg, out, src)

    exp_frames = _expected_source_frames(keep, float(FPS))
    out_y = _frame_luma(out)

    # 1) 帧数必须严格相等
    assert len(out_y) == len(exp_frames) == r["frames"], (
        f"{name}: 帧数不符 成片{len(out_y)} / 期望{len(exp_frames)} / "
        f"render报告{r['frames']}")
    print(f"[CA1] 帧数严格相等：{len(out_y)} 帧   OK")

    # 2) 逐帧内容比对（最关键：证明没有整体/局部错位）
    worst = 0.0
    worst_k = -1
    for k, f in enumerate(exp_frames):
        d = abs(out_y[k] - src_y[f])
        if d > worst:
            worst, worst_k = d, k
    assert worst <= TOL, (
        f"{name}: 第 {worst_k} 帧内容错位，YAVG 偏差 {worst:.3f} > {TOL}"
        f"（成片 {out_y[worst_k]:.2f} vs 源帧 {exp_frames[worst_k]} "
        f"{src_y[exp_frames[worst_k]]:.2f}）")
    print(f"[CA2] 逐帧内容对齐：{len(exp_frames)} 帧全部匹配，"
          f"最大 YAVG 偏差 {worst:.3f}（容差 {TOL}）   OK")

    # 3) 切点接缝校验（隐私安全的直接断言）
    #    每个删除段的接缝处，成片相邻两帧必须正好是「删除段前最后一帧」和
    #    「删除段后第一帧」——即被删片段的首帧内容没有残留在接缝上。
    #    注：源帧亮度存在取值碰撞（1200 帧只有约 180 个 YAVG 取值），因此
    #    不能用「被删亮度是否出现在成片中」来判断，只能做位置精确的接缝校验。
    junctions = 0
    for i in range(len(keep) - 1):
        k = sum(round(e * FPS) - round(s * FPS) for s, e in keep[:i + 1])
        if k <= 0 or k >= len(out_y):
            continue
        f_last = round(keep[i][1] * FPS) - 1        # 接缝前：本段最后一帧
        f_next = round(keep[i + 1][0] * FPS)        # 接缝后：下段第一帧
        f_cut = f_last + 1                          # 被删段的第一帧
        assert abs(out_y[k - 1] - src_y[f_last]) <= TOL, (
            f"{name}: 接缝 {i} 前一帧错位（成片帧 {k-1}）")
        assert abs(out_y[k] - src_y[f_next]) <= TOL, (
            f"{name}: 接缝 {i} 后一帧错位（成片帧 {k}）")
        # 被删段首帧与保留首帧亮度不同时，额外断言接缝上不是被删内容
        if abs(src_y[f_cut] - src_y[f_next]) > TOL:
            assert abs(out_y[k] - src_y[f_cut]) > TOL, (
                f"{name}: 接缝 {i} 残留了被删段首帧（源帧 {f_cut}）")
        junctions += 1
    print(f"[CA3] 隐私安全：{junctions} 个切点接缝均精确衔接，"
          f"无被删画面残留   OK")


def main():
    assert ffmpeg_available(), "需要 ffmpeg"
    work = os.path.join(ROOT, "_accwork")
    os.makedirs(work, exist_ok=True)
    for f in os.listdir(work):
        if f == "ramp_src.mp4":
            continue
        try:
            os.remove(os.path.join(work, f))
        except OSError:
            pass

    print("[准确性] 生成逐帧可辨亮度的合成源 ...")
    src = _synth_ramp_source(work)
    info = probe_video(src)
    print(f"[源] {info['width']}x{info['height']} @ {info['fps']:.3f}fps "
          f"{info['duration']:.1f}s")

    src_y = _frame_luma(src)
    assert len(src_y) >= int(DUR * FPS) - 2, \
        f"源帧数异常：{len(src_y)}"
    # 自检：相邻帧亮度确实可区分（否则本测试无鉴别力）
    diffs = [abs(src_y[i + 1] - src_y[i]) for i in range(len(src_y) - 1)]
    assert min(diffs) > 2 * TOL, \
        f"源相邻帧亮度差过小({min(diffs):.2f})，测试无鉴别力"
    print(f"[源] 逐帧亮度可辨：全片 {len(src_y)} 帧，相邻帧最小差 "
          f"{min(diffs):.2f} > 2×容差 {2 * TOL}   OK")

    # CASE A：少量切点 -> 内联单 select 路径
    a_del = [(2.0 + i * 4.0, 3.0 + i * 4.0) for i in range(8)]
    _case("A_inline", src, work, src_y, a_del, expect_windowed=False)

    # CASE B：大量切点 -> 分窗单命令路径（跨多个 -ss 解码窗）
    b_del = [(0.4 + i * 0.4, 0.6 + i * 0.4) for i in range(95)]
    _case("B_windowed", src, work, src_y, b_del, expect_windowed=True)

    print("\n剪辑准确性测试通过：内联路径与分窗路径均逐帧内容对齐，"
          "被删画面零残留")


if __name__ == "__main__":
    main()
