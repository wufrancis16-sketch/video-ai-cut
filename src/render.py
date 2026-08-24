"""第二阶段：处理（只读 EditPlan，一次 filter_complex + 一次视频编码）。

    plan.json
      ↓ 抽取原始音频（原采样率/声道，音频解码，不涉及视频编码）
      ↓ Python 采样级剪辑：消音+哔声 → 删除拼接 → 片头静音
      ↓ 生成 ASS（原始时间轴，烧录在 select 之前，字幕天然跟随画面）
      ↓ 生成封面 PNG（成片首帧 + 主题花字）
      ↓ **一次** ffmpeg：封面 concat + 视频 select 精确剪辑 + 字幕烧录
        + 已剪辑音频封装  →  一次 libx264 编码
      ↓ 成片 mp4 + 与成片对齐的外挂 SRT

明确不做的事（对应需求「十六」的优先级：隐私安全 > 剪辑准确性 > 音画同步
> 画质 > 速度）：
- 不用 stream copy 生成中间 trimmed.mp4（关键帧对齐会让删除边界不精确，
  隐私片段可能残留几百毫秒）。删除边界一律帧级精确。
- 不做分片多次有损重编码。全片只有一次有损 libx264 编码（极端多切点时
  分桶路径的中间片段用 libx264 -qp 0 无损，最终仍只有一次有损编码）。

视频剪辑为什么用「单 select 内联 → 分窗单命令 → 无损中间片段」三级路径：

  FFmpeg 9.0 有两条硬限制，二者都曾导致真实 OOM / 解析失败：
  (A) 单条 `select` 表达式在 ~90 个 `between()` 组合词项 / ~2304 字符时
      解析期 `Cannot allocate memory`（已实测：n=90 OK / n=100 失败）；
  (B) 多分支 `select` 并行读同一输入，会把 40 分钟源解码 N 次 → 内存 OOM
      （已实测：81 段并行 decode 崩溃 `get_buffer() failed`）。
  两条都不行，因此架构分两层（详见 Hard Rule 9）：

  1) 内联路径（常规视频，切点较少 / 滤镜图 ≤ SAFE_INLINE_GRAPH）：用**单条**
     `select` 表达式 `not(between(t,a,b)+...)` 一次选完全片保留帧——只解码一次
     源，内存有界；表达式短（远低于 ~2304 字符解析上限），不触发 (A)。
  2) 分窗单命令路径（切点多）：按保留区间**索引分窗**（每窗 K=40 段），每窗是
     一个独立的 `-ss/-to -i` 输入（各自独立解码器，只解码自身时间窗，不是「多
     分支读同一输入」，因此不触发 (B)），窗内单条 select 精确剪辑（词项 ≤ K，
     不触发 (A)），concat 后**只编码一次**。无中间文件、无额外编码。
  3) 无损中间片段路径（极端切点，分窗滤镜图仍超命令行长度上限）：逐窗渲染为
     libx264 -qp 0 无损中间片段后 concat，最终仍只有一次有损编码。代价是中间
     文件占用磁盘（≈ 成片时长的无损体积），仅在 (2) 放不下时才启用。

  注：subprocess 不经 cmd.exe，命令行上限是 CreateProcessW 的 32767 字符
  （不是 cmd.exe 的 8191），故 (2) 的容量远大于早期假设。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import audio_edit, cover as cover_mod, subtitle as sub_mod
from . import timeline as T
from .config import Config
from .timeline import EditPlan
from .utils import (ensure_dir, ffmpeg_supports_filter_complex_script,
                     probe_video, run, safe_subtitle_path, ffprobe_json)

Range = Tuple[float, float]

# 内联 filtergraph 安全长度上限（字符数）。FFmpeg 9.0 单条 select 表达式在
# ~2304 字符（~90 个 between 词项）时解析期 OOM（已实测 n=90 OK / n=100 FAIL），
# 故内联路径的表达式 + 固定开销需留足余量。超过此值自动走「分窗单命令」路径，
# 规避解析 OOM。
SAFE_INLINE_GRAPH = 2000
# 分窗渲染时每窗保留段数量上限。每窗表达式约 K*30≈1200 字符（远低于 2304 解析
# 上限），且与切点是否聚集无关（按 keep 索引分窗，而非按时间窗），保证内存安全。
WINDOW_KEEP_SEGMENTS = 40
# 单条 ffmpeg 命令中 filtergraph 的长度上限。Python subprocess 不经过 cmd.exe，
# 直接 CreateProcessW，命令行上限是 32767 字符（不是 cmd.exe 的 8191），给输入
# 路径/编码参数留足余量后取 24000。超过则退化为「无损中间片段」路径。
MAX_CMD_GRAPH = 24000

# ---------------------------------------------------------------------------
# 硬件编码器支持（长视频提速，规避 CPU 软编超时 / OOM）
# ---------------------------------------------------------------------------
_ENC_PROBE_CACHE: Dict[str, bool] = {}

def _ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    return r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

def _probe_hw_encoder(enc: str) -> bool:
    """轻量探测：某硬件编码器运行时是否真正可用（列在 encoders 列表 ≠ 可用）。"""
    if enc in _ENC_PROBE_CACHE:
        return _ENC_PROBE_CACHE[enc]
    ff = _ffmpeg_bin()
    try:
        r = subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
             "-frames:v", "3", "-c:v", enc, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        ok = (r.returncode == 0)
    except Exception:
        ok = False
    _ENC_PROBE_CACHE[enc] = ok
    return ok

def _resolve_encoder(cfg: "Config") -> str:
    enc = (getattr(cfg, "final_encoder", "auto") or "auto").strip().lower()
    if enc in ("h264_qsv", "h264_nvenc", "h264_d3d12va"):
        if not _probe_hw_encoder(enc):
            print(f"  [编码] 指定硬件编码器 {enc} 探测不可用，回退 libx264")
            return "libx264"
        return enc
    if enc == "libx264":
        return "libx264"
    # auto：优先 NVENC，其次 QSV，再 d3d12va，最后回退软编
    for cand in ("h264_nvenc", "h264_qsv", "h264_d3d12va"):
        if _probe_hw_encoder(cand):
            return cand
    return "libx264"

def _video_encode_args(cfg: "Config", fps: float) -> List[str]:
    """返回视频编码参数片段（依据最终选定的编码器）。"""
    enc = _resolve_encoder(cfg)
    if enc == "libx264":
        return [
            "-c:v", "libx264", "-crf", str(cfg.final_crf),
            "-preset", cfg.final_preset, "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-tune", "stillimage" if _is_screen_content(cfg) else "film",
            "-x264-params", "keyint=" + str(max(2, int(round(fps * 2)))),
        ]
    q = getattr(cfg, "final_hw_quality", None) or cfg.final_crf
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-cq", str(q), "-preset", "p4",
                "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}"]
    # h264_qsv / h264_d3d12va 等硬件编码器：global_quality 近似 crf
    return ["-c:v", enc, "-global_quality", str(q),
            "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}"]


# ---------------------------------------------------------------------------
# select 表达式
# ---------------------------------------------------------------------------
def build_video_select_expr(keep: Sequence[Range], delete: Sequence[Range],
                            fps: float, duration: float) -> str:
    """生成视频 select 表达式（帧级精确）。

    区间已对齐帧栅格：a = s*fps、b = e*fps 均为整数。为了让「保留帧数」
    严格等于 (b-a)（而不是 b-a+1），判定区间取半帧内缩：
        between(t, (a-0.25)/fps, (b-0.75)/fps)
    这样帧 a..b-1 被保留，帧 b 归下一个区间，边界不会重复也不会漏。

    表达式采用「保留式」或「删除式」中更短的一种，减小表达式规模。
    """
    if not keep:
        return "0"
    if len(keep) == 1 and keep[0][0] <= 1e-6 and keep[0][1] >= duration - 1e-6:
        return ""                      # 全片保留 -> 无需 select

    def terms(ranges: Sequence[Range]) -> str:
        parts = []
        for s, e in ranges:
            a = round(s * fps)
            b = round(e * fps)
            if b <= a:
                continue
            lo = (a - 0.25) / fps
            hi = (b - 0.75) / fps
            parts.append(f"between(t,{lo:.6f},{hi:.6f})")
        return "+".join(parts)

    keep_expr = terms(keep)
    if delete and len(delete) < len(keep):
        del_expr = terms(delete)
        if del_expr:
            alt = f"not({del_expr})"
            if len(alt) < len(keep_expr):
                return alt
    return keep_expr


def expected_output_frames(keep: Sequence[Range], fps: float) -> int:
    return sum(max(0, round(e * fps) - round(s * fps)) for s, e in keep)


def _video_chain_single(keep: Sequence[Range], delete: Sequence[Range],
                         fps: float, duration: float,
                         vf_pre: Sequence[str],
                         W: Optional[int] = None, H: Optional[int] = None) -> Tuple[str, str]:
    """视频剪辑链：单条 select 表达式（内存安全，仅解码一次源）。

    用 `build_video_select_expr` 生成单条 `select='...'`（保留式或删除式），
    整片保留帧一次性选出，再 `setpts=N/FRAME_RATE/TB` 重排帧号。相比「多 select 并行
    读同一输入」，本方式只解码一次源，内存有界，彻底规避 FFmpeg 9.0 并行 decode
    OOM；表达式长度受 `SAFE_INLINE_GRAPH` 约束，超长时由 `_build_windowed_command`
    的分窗单命令路径接管，规避单条表达式解析 OOM。

    vf_pre 为 select 之前的预处理滤镜（如 fps= / subtitles=），对所有段共用。
    返回 (滤镜链字符串, 输出标签)。
    """
    if not keep:
        return "", "[0:v]"
    head = ",".join(vf_pre) + "," if vf_pre else ""
    expr = build_video_select_expr(keep, delete, fps, duration)
    _tail = "format=yuv420p,setsar=1"
    if W and H:
        _tail = f"scale={W}:{H},{_tail}"
    if expr == "":
        # 全片保留：无需 select，直接走 head + 归一化
        chain = f"[0:v]{head}{_tail}[vbody]"
    elif expr == "0":
        # 全删（render 已拒绝，这里兜底）
        chain = f"[0:v]{head}select='0',{_tail}[vbody]"
    else:
        # 必须用 setpts=N/{fps}/TB 重排帧号：select 丢帧后剩余帧仍保留
        # 原始 PTS，`PTS-STARTPTS` 只减去首帧偏移、**不会压掉丢帧留下的时间
        # 空洞**，输出按 CFR 会把空洞补成重复帧（实测 960 帧被补回 1200 帧）。
        # 注意必须用**显式 fps**（而非 FRAME_RATE 常数）：FRAME_RATE 取流标称帧率
        # （如 30/1），而本链路用平均帧率（如 30.298971）计算帧数，两者不一致会让
        # 成片被 `-r fps` 复制 ~1% 帧，导致视频比音频长（40 分钟片漂移 ~20s）。
        chain = (f"[0:v]{head}select='{expr}',"
                 f"setpts=N/{fps:.6f}/TB,{_tail}[vbody]")
    return chain, "[vbody]"


def _inline_graph_len(keep: Sequence[Range], delete: Sequence[Range],
                      fps: float, duration: float,
                      has_audio: bool) -> int:
    """估算内联单 select 滤镜图长度，用于三级路径选择（内联 / 分窗单命令 / 无损分片）。

    与 render() 实际内联路径的构造保持一致（视频链 + 可选音频链），但忽略封面
    与字幕（仅影响固定开销，误差 < 200 字符，对 2000 阈值判断无影响）。
    """
    vchain, _ = _video_chain_single(
        keep, delete, fps, duration, [f"fps={fps:.6f}"])
    total = len(vchain)
    if has_audio:
        total += len("[0:a]aformat=sample_fmts=fltp:sample_rates=48000[outa]")
    return total


# ---------------------------------------------------------------------------
# 渲染主流程
# ---------------------------------------------------------------------------
def render(plan: EditPlan, cfg: Config, out_path: str,
           video: Optional[str] = None) -> Dict[str, Any]:
    """按 plan 渲染成片。返回渲染信息字典。"""
    src = os.path.abspath(video or plan.source)
    if not os.path.exists(src):
        raise RuntimeError(f"源视频不存在: {src}")
    ensure_dir(cfg.workdir)
    ensure_dir(os.path.dirname(os.path.abspath(out_path)) or ".")

    info = probe_video(src)
    fps = float(plan.fps or info["fps"])
    W, H = int(plan.width or info["width"]), int(plan.height or info["height"])

    plan.normalize()
    keep = plan.keep_ranges(snap=True)
    dels = plan.delete_ranges(snap=True)
    mutes = plan.mute_ranges(snap=True)
    if not keep:
        raise RuntimeError("剪辑方案会删除全部内容，拒绝渲染")

    has_speed = bool(plan.speed_segments)
    if has_speed:
        pieces = plan.pieces(snap=True)
        # 变速后成片帧数：每段按 (时长/speed) 计算，保证与音频严格对齐
        n_frames = sum(max(0, round((p["end"] - p["start"]) / p["speed"] * fps))
                       for p in pieces)
        speed_pieces = [(p["start"], p["end"], float(p["speed"])) for p in pieces]
        print("[渲染] 检测到变速段：使用分段渲染路径（仍然只编码一次，"
              "音视频同步变速）")
    else:
        pieces = None
        n_frames = expected_output_frames(keep, fps)
        speed_pieces = None
    body_dur = n_frames / fps

    print(f"[渲染] 保留 {len(keep)} 段 / 删除 {len(dels)} 段 / 消音 "
          f"{len(mutes)} 段")
    print(f"[渲染] 目标：{W}x{H} @ {fps:.3f}fps，{n_frames} 帧 "
          f"({body_dur:.3f}s)")

    # ---- 1) 音频：采样级精确剪辑 ---------------------------------------
    audio_path = None
    audio_meta: Dict[str, Any] = {}
    if info["has_audio"]:
        raw_wav = os.path.join(cfg.workdir, "render_src.wav")
        if not (os.path.exists(raw_wav) and os.path.getsize(raw_wav) > 1000):
            print("  [音频] 提取原始音频（保持采样率与声道）")
            run(["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
                 "-ar", str(info["sample_rate"] or cfg.final_audio_sr),
                 "-ac", str(max(1, info["channels"] or 1)), raw_wav])
        edited = os.path.join(cfg.workdir, "render_audio.wav")
        audio_meta = audio_edit.render_audio(
            raw_wav, edited,
            keep_ranges=keep, mute_ranges=mutes,
            beep_freq=cfg.beep_freq, beep_volume=getattr(cfg, "beep_volume", 0.2),
            lead_silence=_cover_seconds(plan, cfg, fps),
            expect_duration=_cover_seconds(plan, cfg, fps) + body_dur,
            speed_pieces=speed_pieces,
        )
        audio_path = audio_meta["path"]
        print(f"  [音频] 消音 {audio_meta['mute_applied']} 段 / 拼接点 "
              f"{audio_meta['joins']} 处 / 时长 {audio_meta['duration']:.3f}s")
    else:
        print("  [音频] 源视频无音轨，成片将无音轨")

    # ---- 2) 字幕：ASS（原始时间轴，烧录在 select 之前）----------------
    ass_path = None
    if (cfg.burn_subtitle and plan.subtitle and plan.subtitle_cues
            and not plan.existing_subtitle):
        ass_path = os.path.join(cfg.workdir, "burn.ass")
        sub_mod.write_ass(plan.subtitle_cues, W, H, cfg, ass_path)
        print(f"  [字幕] 烧录 {len(plan.subtitle_cues)} 条（原始时间轴，"
              f"随帧丢弃自动对齐）")
    elif plan.existing_subtitle:
        print(f"  [字幕] 跳过：原视频已含字幕（{plan.existing_subtitle['type']}），"
              f"不再烧录新字幕")

    # ---- 3) 封面 -------------------------------------------------------
    cover_png = _make_cover(plan, cfg, src, W, H)

    # ---- 4) 渲染：内联单 select / 分窗单命令 / 无损中间片段（三级路径）----
    cmd, graph = _build_command(
        src=src, out_path=out_path, cfg=cfg, plan=plan,
        keep=keep, dels=dels, fps=fps, W=W, H=H,
        duration=float(plan.duration), n_frames=n_frames,
        audio_path=audio_path, ass_path=ass_path, cover_png=cover_png,
        has_speed=has_speed,
    )
    graph_file = None
    cover_frames = (max(1, int(round(float(cfg.cover_duration) * fps)))
                   if cover_png else 0)

    # 字幕文件路径在 filtergraph 内是「纯文件名」(见 _build_command)，需把工作
    # 目录切到 workdir，ffmpeg 才能按相对名找到 burn.ass。所有 -i 输入(源视频/
    # 音频/封面)与 out_path 都是绝对路径，chdir 不影响它们。
    def _run_in_workdir(c: List[str]) -> None:
        prev_cwd = os.getcwd()
        os.chdir(cfg.workdir)
        try:
            run(c)
        finally:
            os.chdir(prev_cwd)

    if len(graph) > SAFE_INLINE_GRAPH and not has_speed:
        # 单条 select 表达式超过 FFmpeg 9.0 的解析安全上限：改走分窗路径。
        # 优先「分窗单命令」（无中间文件、仍只编码一次），滤镜图仍超命令行
        # 长度上限时才退化为「无损中间片段」。
        wcmd, wgraph = _build_windowed_command(
            src, out_path, cfg, plan, keep, fps, W, H,
            audio_path, ass_path, cover_png)
        n_win = len(_keep_windows(keep, fps))
        if len(wgraph) <= MAX_CMD_GRAPH:
            print(f"  [编码] 单 select 滤镜图 {len(graph)} 字符超解析安全上限 "
                  f"{SAFE_INLINE_GRAPH}，切换分窗单命令路径")
            print(f"  [编码] {n_win} 个解码窗（每窗只解码自身时间段），"
                  f"滤镜图 {len(wgraph)} 字符，全片仍只编码一次")
            _run_in_workdir(wcmd)
        else:
            print(f"  [编码] 分窗滤镜图 {len(wgraph)} 字符超命令行上限 "
                  f"{MAX_CMD_GRAPH}，退化为分窗无损中间片段路径")
            _render_chunked_video(
                plan, cfg, src, out_path, keep, fps, W, H,
                audio_path, ass_path, cover_png, cover_frames)
    else:
        cmd = cmd[:]
        if ffmpeg_supports_filter_complex_script():
            # 优先用 -filter_complex_script：把滤镜图写文件，规避 Windows 命令行
            # 长度上限（约 8191 字符）。
            graph_file = os.path.join(cfg.workdir, "filtergraph.txt")
            with open(graph_file, "w", encoding="utf-8") as f:
                f.write(graph)
            cmd[cmd.index("__GRAPH__")] = graph_file
            print(f"  [编码] 视频编码器 {_resolve_encoder(cfg)}（全片唯一一次编码，"
                  f"滤镜图走 filter_complex_script）")
        else:
            # 回退：内联 -filter_complex。单条 select 滤镜图天然紧凑，
            # 常规视频远小于命令行上限；多切点场景会触发上面的分窗路径。
            cmd[cmd.index("-filter_complex_script")] = "-filter_complex"
            cmd[cmd.index("__GRAPH__")] = graph
            print(f"  [编码] 视频编码器 {_resolve_encoder(cfg)}（全片唯一一次编码，"
                  f"内联 filter_complex，滤镜图 {len(graph)} 字符）")

        _run_in_workdir(cmd)

    # ---- 5) 校验 + 外挂字幕 --------------------------------------------
    result = _verify(out_path, plan, cfg, fps, n_frames, cover_png)
    if cfg.keep_external_subtitle and plan.subtitle_cues and not plan.existing_subtitle:
        srt = os.path.splitext(out_path)[0] + ".srt"
        cues = plan.remap_cues()
        lead = _cover_seconds(plan, cfg, fps)
        if lead > 1e-6:
            cues = [{**c, "start": c["start"] + lead, "end": c["end"] + lead}
                    for c in cues]
        sub_mod.write_srt(cues, srt)
        result["srt"] = srt
        print(f"  [字幕] 外挂 SRT（已按成片时间轴重算）-> "
              f"{os.path.basename(srt)}")
    result["audio"] = audio_meta
    result["graph_file"] = graph_file
    return result


# ---------------------------------------------------------------------------
def _cover_seconds(plan: EditPlan, cfg: Config, fps: float) -> float:
    """封面片头的**精确**时长（整帧数），0 表示不加封面。"""
    if not plan.cover or not cfg.cover_duration:
        return 0.0
    if plan.cover.get("disabled"):
        return 0.0
    frames = max(1, int(round(float(cfg.cover_duration) * fps)))
    return frames / fps


def _make_cover(plan: EditPlan, cfg: Config, src: str, W: int,
                H: int) -> Optional[str]:
    """抽成片首帧生成封面 PNG。失败不影响主流程。"""
    if not plan.cover or plan.cover.get("disabled"):
        return None
    try:
        keep = plan.keep_ranges(snap=True)
        ts = float(plan.cover.get("frame_ts") or 0.0)
        # 确保取帧点落在保留区间内（不能用被删掉的画面做封面）
        if keep and not any(s <= ts <= e for s, e in keep):
            ts = keep[0][0] + 0.2
        frame = os.path.join(cfg.workdir, "cover_frame.png")
        run(["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", src,
             "-frames:v", "1", "-q:v", "2", frame])
        title = (plan.cover.get("title") or "").strip()
        if not title:
            print("  [封面] 无标题，使用纯首帧")
        out_png = os.path.join(cfg.workdir, "cover.png")
        cover_mod.generate_cover(
            frame, title, out_png, width=W, height=H,
            style=cfg.cover_style, font_size=cfg.cover_font_size,
            title_position=cfg.cover_title_position,
            bg_darken=cfg.cover_bg_darken,
            cover_duration=cfg.cover_duration)
        print(f"  [封面] 产品页帧 @{ts:.2f}s + 主题花字「{title or '(无)'}」")
        return out_png
    except Exception as e:  # noqa
        print(f"  [warn] 封面生成失败，跳过片头: {e}")
        return None


def _build_command(src: str, out_path: str, cfg: Config, plan: EditPlan,
                   keep: List[Range], dels: List[Range], fps: float,
                   W: int, H: int, duration: float, n_frames: int,
                   audio_path: Optional[str], ass_path: Optional[str],
                   cover_png: Optional[str], has_speed: bool
                   ) -> Tuple[List[str], str]:
    """组装 ffmpeg 命令与 filtergraph。"""
    inputs: List[str] = ["-i", src]
    idx_src = 0
    idx_audio = idx_cover = -1

    if audio_path:
        inputs += ["-i", audio_path]
        idx_audio = len(inputs) // 2 - 1

    cover_frames = 0
    if cover_png:
        cover_frames = max(1, int(round(float(cfg.cover_duration) * fps)))
        # 多给 0.5s 素材，随后用 trim=end_frame 精确取整帧数
        inputs += ["-loop", "1", "-framerate", f"{fps:.6f}",
                   "-t", f"{cover_frames / fps + 0.5:.3f}", "-i", cover_png]
        idx_cover = (len([x for x in inputs if x == "-i"])) - 1

    chains: List[str] = []

    # ---- 视频链 ----
    vf_pre: List[str] = []
    if cfg.force_cfr:
        vf_pre.append(f"fps={fps:.6f}")          # 统一 CFR，保证切点帧级精确
    if ass_path:
        # 字幕文件放在 workdir 下，用「纯文件名」引用：Windows 盘符冒号(C:)
        # 会破坏 filtergraph 的 ':' 选项分隔解析。render() 在编码前会
        # os.chdir(workdir)，因此此处用 basename 即可（见 SKILL.md 硬规则 9）。
        vf_pre.append(f"subtitles={safe_subtitle_path(os.path.basename(ass_path))}")

    if has_speed:
        vchain, vbody = _speed_video_chain(plan, vf_pre, fps)
        chains.append(vchain)
    else:
        vchain, vbody = _video_chain_single(keep, dels, fps, duration, vf_pre, W=W, H=H)
        chains.append(vchain)

    # ---- 封面拼接（同一次编码内完成）----
    if cover_png:
        chains.append(
            f"[{idx_cover}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps:.6f},"
            f"trim=end_frame={cover_frames},setpts=N/FRAME_RATE/TB,"
            f"format=yuv420p,setsar=1[vcover]")
        chains.append(f"[vcover]{vbody}concat=n=2:v=1:a=0[outv]")
        vout = "[outv]"
    else:
        vout = vbody

    # ---- 音频链：已在 Python 中剪辑完成，这里只做格式统一 ----
    aout = None
    if audio_path:
        chains.append(
            f"[{idx_audio}:a]aformat=sample_fmts=fltp:"
            f"sample_rates={cfg.final_audio_sr}[outa]")
        aout = "[outa]"

    graph = ";\n".join(chains)

    cmd: List[str] = ["ffmpeg", "-y", *inputs,
                      "-filter_complex_script", "__GRAPH__",
                      "-map", vout]
    if aout:
        cmd += ["-map", aout]
    cmd += _video_encode_args(cfg, fps)
    if aout:
        cmd += ["-c:a", "aac", "-b:a", cfg.final_audio_bitrate,
                "-ar", str(cfg.final_audio_sr)]
        cmd += ["-shortest"] if False else []
    cmd += ["-movflags", "+faststart", "-map_metadata", "-1", out_path]
    return cmd, graph


def _keep_windows(keep: Sequence[Range], fps: float
                  ) -> List[Tuple[float, float, List[Range]]]:
    """按保留区间**索引**分窗，返回 [(解码起点 ss, 解码终点 to, 窗内保留段)]。

    按索引（而非固定时长）分窗，保证每窗 select 词项数 ≤ WINDOW_KEEP_SEGMENTS，
    与切点是否聚集无关。解码窗前后各留 2 帧余量，容纳取整与关键帧前移。
    """
    lead = trail = 2.0 / fps
    out: List[Tuple[float, float, List[Range]]] = []
    for i in range(0, len(keep), WINDOW_KEEP_SEGMENTS):
        wk = list(keep[i:i + WINDOW_KEEP_SEGMENTS])
        if not wk:
            continue
        g_start = min(s for s, _ in wk)
        g_end = max(e for _, e in wk)
        out.append((max(0.0, g_start - lead), g_end + trail, wk))
    return out


def _window_vf(ss: float, wk: List[Range], fps: float, duration: float,
               ass_path: Optional[str],
               W: Optional[int] = None, H: Optional[int] = None) -> Optional[str]:
    """单个解码窗的视频滤镜链（不含输入/输出标签）。

    `-ss` 输入定位会把解码后的时间戳**重置为 0**（已实测）。若直接用窗内局部
    时间 select，烧录字幕（ASS 是原始时间轴）会整体偏移 ss 秒。因此解码后先
    `setpts=PTS+ss/TB` 把时间轴搬回原始位置，再按**原始时间**做 select——已实测
    该组合帧级精确（选出帧数与半帧内缩公式预期完全一致）。

    注意：**这里故意不用 `fps=` 滤镜**。原因：本路径是「输入定位解码窗」，当某窗
    的解码范围延伸到源末尾（最后一窗的 `to` 越过源时长）时，`fps` 滤镜在流末会
    丢弃最后一帧（fps 滤镜需下一帧来 flush 末帧，而 EOF 已到），实测导致成片少 1
    帧。源本身是 CFR（我们重编码的产物），解码后帧已落在精确 1/fps 栅格上，
    `setpts=PTS+ss/TB` 后按原始时间 select 即帧级精确；`setpts=N/{fps}/TB`
    重排 + 最终 `-r {fps}` 编码保证成片仍是 CFR。用 `fps` 滤镜反而画蛇添足且丢帧。
    注意：重排必须写**显式 fps** 而非 `N/FRAME_RATE`——FRAME_RATE 取的是流标称帧率
    （30/1），与本链路平均帧率（30.298971）不符，会让 `-r` 复制 ~1% 帧造成音画
    时长漂移（40 分钟片尾差 ~20s，实测 2026-08-19 修复）。
    """
    expr = build_video_select_expr(wk, [], fps, duration)
    if expr == "0":
        return None
    vf = f"setpts=PTS+{ss:.6f}/TB"
    if ass_path:
        vf += f",subtitles={safe_subtitle_path(os.path.basename(ass_path))}"
    if expr != "":
        vf += f",select='{expr}'"
    tail = f"setpts=N/{fps:.6f}/TB,format=yuv420p,setsar=1"
    if W and H:
        tail = f"scale={W}:{H},{tail}"
    return vf + "," + tail


def _build_windowed_command(src: str, out_path: str, cfg: Config,
                            plan: EditPlan, keep: List[Range], fps: float,
                            W: int, H: int, audio_path: Optional[str],
                            ass_path: Optional[str],
                            cover_png: Optional[str]
                            ) -> Tuple[List[str], str]:
    """分窗**单命令**渲染：每窗一个 `-ss/-to` 输入 + 单条 select，concat 后只编码一次。

    相比「无损中间片段」路径，本路径：
      - 不产生任何中间文件（长视频的无损中间片段可达数 GB）；
      - 全程只有一次编码（连无损中间编码也省掉），更快且画质更优；
      - 每个输入只解码自己的时间窗（内存有界），每条 select 词项 ≤ K（不触发
        FFmpeg 9.0 表达式解析 OOM）；
      - 各输入是**独立解码器**，不是「多分支读同一输入」，因此不会出现
        并行 N 倍解码 OOM。
    唯一约束是整张 filtergraph 需在命令行长度内（见 MAX_CMD_GRAPH）。
    """
    duration = float(plan.duration)
    windows = _keep_windows(keep, fps)
    inputs: List[str] = []
    chains: List[str] = []
    wlabels: List[str] = []
    n_in = 0

    for wi, (ss, to, wk) in enumerate(windows):
        vf = _window_vf(ss, wk, fps, duration, ass_path, W=W, H=H)
        if vf is None:
            continue
        inputs += ["-ss", f"{ss:.6f}", "-to", f"{to:.6f}", "-i", src]
        chains.append(f"[{n_in}:v]{vf}[w{wi}]")
        wlabels.append(f"[w{wi}]")
        n_in += 1

    if not wlabels:
        raise RuntimeError("分窗渲染：没有任何有效片段")

    if len(wlabels) == 1:
        vbody = wlabels[0]
    else:
        chains.append(f"{''.join(wlabels)}concat=n={len(wlabels)}:v=1:a=0"
                      f"[vbody]")
        vbody = "[vbody]"

    idx_audio = -1
    if audio_path:
        inputs += ["-i", audio_path]
        idx_audio = n_in
        n_in += 1

    if cover_png:
        cover_frames = max(1, int(round(float(cfg.cover_duration) * fps)))
        inputs += ["-loop", "1", "-framerate", f"{fps:.6f}",
                   "-t", f"{cover_frames / fps + 0.5:.3f}", "-i", cover_png]
        idx_cover = n_in
        n_in += 1
        chains.append(
            f"[{idx_cover}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps:.6f},"
            f"trim=end_frame={cover_frames},setpts=N/FRAME_RATE/TB,"
            f"format=yuv420p,setsar=1[vcover]")
        chains.append(f"[vcover]{vbody}concat=n=2:v=1:a=0[outv]")
        vout = "[outv]"
    else:
        vout = vbody

    aout = None
    if audio_path:
        chains.append(f"[{idx_audio}:a]aformat=sample_fmts=fltp:"
                      f"sample_rates={cfg.final_audio_sr}[outa]")
        aout = "[outa]"

    graph = ";".join(chains)
    cmd: List[str] = ["ffmpeg", "-y", *inputs,
                      "-filter_complex", graph, "-map", vout]
    if aout:
        cmd += ["-map", aout]
    cmd += _video_encode_args(cfg, fps)
    if aout:
        cmd += ["-c:a", "aac", "-b:a", cfg.final_audio_bitrate,
                "-ar", str(cfg.final_audio_sr)]
    cmd += ["-movflags", "+faststart", "-map_metadata", "-1", out_path]
    return cmd, graph


def _render_chunked_video(plan: EditPlan, cfg: Config, src: str, out_path: str,
                          keep: List[Range], fps: float, W: int, H: int,
                          audio_path: Optional[str], ass_path: Optional[str],
                          cover_png: Optional[str], cover_frames: int) -> None:
    """分窗 decode-once 渲染（内联单 select 滤镜图超长时的回退路径）。

    根因（已实测）：
      (A) FFmpeg 9.0 单条 select 表达式 ~90 词项/ ~2304 字符解析期 OOM；
      (B) 多 select 并行读同一输入会把源解码 N 次 -> 内存 OOM。
    二者都不可行，因此本路径**按保留区间索引分窗**（每窗 K=WINDOW_KEEP_SEGMENTS
    段），每窗仅用 `-ss/-to` 解码其对应时间窗（内存有界，不重解码整片），窗内仍用
    **单条** select 精确剪辑 + `setpts=N/FRAME_RATE/TB` 归零；最后 concat 所有窗
    片段 + 音频 + 封面 -> 一次有损编码。

    内存安全：任一时刻只有一个 ffmpeg 进程、只解码一个时间窗（~源时长/窗数），
    彻底规避 (B) 的并行 N 倍解码；每窗表达式是单条 select 且词项 ≤ K（远 < 90），
    规避 (A) 解析 OOM。质量：仅最终一遍有损，中间窗片段 libx264 -qp 0 无损。
    """
    work = cfg.workdir
    tmp_clips: List[str] = []
    inputs: List[str] = []
    idx = 0

    def _chdir_run(cmd: List[str]):
        prev = os.getcwd()
        os.chdir(work)
        try:
            run(cmd)
        finally:
            os.chdir(prev)

    # 可选：封面作为首段（与窗片段同参数，保证 concat 兼容）
    if cover_png and cover_frames > 0:
        cover_clip = os.path.join(work, "_chunk_cover.mp4")
        _chdir_run([
            "ffmpeg", "-y", "-loop", "1", "-framerate", f"{fps:.6f}",
            "-t", f"{cover_frames / fps + 0.5:.3f}", "-i", cover_png,
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps:.6f},"
                   f"trim=end_frame={cover_frames},setpts=N/FRAME_RATE/TB,"
                   f"format=yuv420p,setsar=1",
            "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}", cover_clip,
        ])
        tmp_clips.append(cover_clip)
        inputs += ["-i", cover_clip]
        idx += 1

    for wi, (ss, to, wk) in enumerate(_keep_windows(keep, fps)):
        vf = _window_vf(ss, wk, fps, float(plan.duration), ass_path)
        if vf is None:
            continue
        clip = os.path.join(work, f"_wchunk_{wi}.mp4")
        _chdir_run([
            "ffmpeg", "-y", "-ss", f"{ss:.6f}", "-to", f"{to:.6f}",
            "-i", src, "-vf", vf, "-an",
            "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}", clip,
        ])
        tmp_clips.append(clip)
        inputs += ["-i", clip]
        idx += 1

    if idx == 0:
        raise RuntimeError("分窗渲染：没有任何有效片段")

    # 最终 concat + 编码（唯一一次有损编码）
    vlabels = "".join(f"[{i}:v]" for i in range(idx))
    fgraph = f"{vlabels}concat=n={idx}:v=1:a=0[v]"
    fcmd: List[str] = ["ffmpeg", "-y", *inputs]
    if audio_path:
        audio_in = idx
        fcmd += ["-i", audio_path, "-filter_complex", fgraph,
                 "-map", "[v]", "-map", f"{audio_in}:a"]
    else:
        fcmd += ["-filter_complex", fgraph, "-map", "[v]"]
    fcmd += _video_encode_args(cfg, fps)
    if audio_path:
        fcmd += ["-c:a", "aac", "-b:a", cfg.final_audio_bitrate,
                 "-ar", str(cfg.final_audio_sr)]
    fcmd += ["-movflags", "+faststart", "-map_metadata", "-1", out_path]
    run(fcmd)

    # 清理中间片段
    for t in tmp_clips:
        try:
            os.remove(t)
        except OSError:
            pass


def _is_screen_content(cfg: Config) -> bool:
    """ERP 界面/表格/文字为主 -> stillimage tune 更利于保住文字锐度。"""
    return bool(getattr(cfg, "tune_screen_content", True))


def _speed_video_chain(plan: EditPlan, pre_filters: List[str],
                       fps: float) -> str:
    """变速路径：按 pieces 分段 trim+setpts 后 concat（仍然只编码一次）。

    仅在存在 speed_segments 时使用。片段较多时比 select 路径慢，
    因此默认 pause_mode=trim 不会走到这里。
    """
    pieces = plan.pieces(snap=True)
    n = len(pieces)
    parts: List[str] = []
    head = ",".join(pre_filters) if pre_filters else ""
    src_label = "[0:v]"
    if head:
        parts.append(f"{src_label}{head}[vpre]")
        src_label = "[vpre]"
    labels = "".join(f"[vs{i}]" for i in range(n))
    parts.append(f"{src_label}split={n}{labels}")
    cat_in = []
    for i, p in enumerate(pieces):
        sp = p["speed"]
        setpts = f"(1/{sp})*(PTS-STARTPTS)" if sp != 1.0 else "PTS-STARTPTS"
        parts.append(f"[vs{i}]trim=start={p['start']:.6f}:end={p['end']:.6f},"
                     f"setpts={setpts}[vp{i}]")
        cat_in.append(f"[vp{i}]")
    # 关键：变速后**不能**再 `setpts=N/FRAME_RATE/TB` 重编号——否则会把
    # `setpts=(1/speed)*(PTS-STARTPTS)` 的压缩效果抵消，变速失效（实测输出
    # 时长与源一致、加速段毫无作用）。各片段经 setpts 后已是「相对成片时间轴」
    # 的连续 PTS，concat 后直接 format/setsar，由输出 `-r fps` 归一成 CFR。
    parts.append("".join(cat_in) +
                 f"concat=n={n}:v=1:a=0,"
                 f"format=yuv420p,setsar=1[vbody]")
    return ";\n".join(parts), "[vbody]"


# ---------------------------------------------------------------------------
def _verify(out_path: str, plan: EditPlan, cfg: Config, fps: float,
            n_frames: int, cover_png: Optional[str]) -> Dict[str, Any]:
    """成片校验：分辨率/帧率保持、时长符合预期、音画时长一致。"""
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError(f"渲染失败：输出文件异常 {out_path}")
    info = probe_video(out_path)
    cover_dur = _cover_seconds(plan, cfg, fps) if cover_png else 0.0
    expect = cover_dur + n_frames / fps

    issues: List[str] = []
    if info["width"] != plan.width or info["height"] != plan.height:
        issues.append(f"分辨率变化 {plan.width}x{plan.height} -> "
                      f"{info['width']}x{info['height']}")
    if abs(info["fps"] - fps) > 0.05:
        issues.append(f"帧率变化 {fps:.3f} -> {info['fps']:.3f}")
    if abs(info["duration"] - expect) > 0.5:
        issues.append(f"时长偏差：预期 {expect:.3f}s，实际 "
                      f"{info['duration']:.3f}s")
    if plan.meta.get("has_audio") and not info["has_audio"]:
        issues.append("成片丢失音轨")

    # 音视频时长一致性（无黑屏/无断音/无漂移的兜底校验）：
    # 视频流与音频流时长差 > 0.15s 意味着尾部缺帧（黑屏/冻结）或音频多出
    # （静音尾巴/断音），说明 select 帧数与 audio_edit 拼接样本数没对齐。
    if info["has_audio"]:
        try:
            raw = ffprobe_json(out_path)
            vdur = adur = None
            for s in raw.get("streams", []):
                if s.get("codec_type") == "video" and vdur is None:
                    try:
                        vdur = float(s.get("duration") or 0) or None
                    except (TypeError, ValueError):
                        vdur = None
                elif s.get("codec_type") == "audio" and adur is None:
                    try:
                        adur = float(s.get("duration") or 0) or None
                    except (TypeError, ValueError):
                        adur = None
            if vdur and adur and abs(vdur - adur) > 0.15:
                issues.append(f"音画时长不一致：视频 {vdur:.3f}s vs "
                              f"音频 {adur:.3f}s（可能黑屏/断音/漂移）")
        except Exception:  # noqa
            pass

    for i in issues:
        print(f"  [warn] {i}")
    print(f"[渲染] 完成 {os.path.basename(out_path)}  "
          f"{info['width']}x{info['height']} @ {info['fps']:.3f}fps  "
          f"{info['duration']:.3f}s（预期 {expect:.3f}s）")
    return {
        "output": out_path,
        "width": info["width"], "height": info["height"],
        "fps": info["fps"], "duration": info["duration"],
        "expected_duration": expect,
        "cover_duration": cover_dur,
        "frames": n_frames,
        "has_audio": info["has_audio"],
        "issues": issues,
    }
