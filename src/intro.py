"""检测并裁剪「腾讯会议开场」片段。

开场 = 蓝底白字屏（极短，常 0.1s）+ 随后的「会议中」白底等候室界面，
直到用户开始共享屏幕（软件界面出现）为止。整段从成片开头删除，
封面也从此后的软件画面抽取。

检测分两段：
  1) 蓝色屏：多区域投票（见 _frame_is_blue）。
  2) 白底会议室 → 软件界面 的转折：会议室画面色彩少、亮度方差低；
     共享屏幕（软件）后色彩占比与亮度方差明显升高。以「会议室基线」
     的相对跳变（持续 ≥ 1.5s）作为会议结束点。

颜色判定（多区域投票，避免白色文字/亮色装饰把全帧均值拉到灰白）：
    将帧划分为 6 个区域（四角+中上+中下），分别判断是否为蓝色主导
    （B-R > blue_thr 且 B-G > blue_thr 且 B > 80）。
    超半数区域判定为蓝色 → 该帧视为蓝色帧。
    白色/浅灰屏（如 R≈G≈B≈200）不会触发，避免误删。

裁剪方式：用 ffmpeg 重新编码去掉 [0, intro_end]，保证精确切到目标时间点
（无损 copy 在 GOP 边界会切不准）。只对整个视频做一次，结果缓存在 workdir。
"""
from __future__ import annotations

import glob
import os
import shutil
import statistics
import subprocess
import tempfile
from typing import List, Optional, Tuple

from PIL import Image

from .utils import run, get_duration
from . import screen_inspect as si


# 腾讯会议界面/等候室专属文字（用于在**无蓝屏**时识别会议开场，精确匹配）。
# 这些词只出现在腾讯会议 app 界面（等候室/快速会议/会议进行中），
# 好生意等软件界面不会出现；"共享屏幕"是录屏水印常见词，仅作辅助。
MEETING_TEXT_KEYS = [
    "腾讯会议", "会议号", "快速会议", "预定会议", "加入会议",
    "创建者", "开始录制", "离开会议", "结束会议",
]


def _frame_is_meeting_text(path: str) -> bool:
    """OCR 判断一帧是否为腾讯会议界面/等候室（整帧 + 左栏文字）。

    精确匹配（k in joined），不用模糊匹配——避免好生意等 ERP 界面
    形近词（日期/协议…）误判。OCR 失败时返回 False（安全，不裁剪）。
    """
    try:
        from . import screen_inspect as si
        full = "".join(si._ocr_full_texts(path))
        left = "".join(si._ocr_wechat_texts(path))
    except Exception:
        return False
    joined = full + left
    return any(k in joined for k in MEETING_TEXT_KEYS)


def _region_rgb(path: str) -> List[Tuple[float, float, float]]:
    """将帧分为 6 个区域，返回每个区域的平均 RGB。"""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    # 6 个采样区域（避开边缘可能的播放器边框/黑边）
    regions = [
        (w // 10, h // 10, w // 4, h // 3),          # 左上
        (w * 7 // 10, h // 10, w - w // 10, h // 4), # 右上（避开右侧 3D 立方体）
        (w // 10, h * 2 // 3, w // 4, h - h // 10),  # 左下
        (w // 2, h // 10, w * 3 // 5, h // 3),       # 中上（文字右侧空白）
        (w // 2, h * 2 // 3, w * 3 // 5, h - h // 10), # 中下
        (w // 2, h // 2, w * 3 // 5, h * 2 // 3),     # 正中
    ]
    results = []
    for x1, y1, x2, y2 in regions:
        crop = img.crop((x1, y1, x2, y2))
        px = list(crop.getdata())
        n = len(px)
        if n == 0:
            continue
        r = sum(p[0] for p in px) / n
        g = sum(p[1] for p in px) / n
        b = sum(p[2] for p in px) / n
        results.append((r, g, b))
    return results


def _is_blue(r: float, g: float, b: float, thr: float) -> bool:
    return (b - r) > thr and (b - g) > thr and b > 80


def _frame_is_blue(path: str, blue_thr: float,
                   vote_ratio: float = 0.5) -> bool:
    """多区域投票：超 vote_ratio 比例的区域为蓝色 → 帧判定为蓝色。"""
    rgbs = _region_rgb(path)
    if not rgbs:
        return False
    blue_count = sum(1 for r, g, b in rgbs if _is_blue(r, g, b, blue_thr))
    return blue_count / len(rgbs) >= vote_ratio


def _frame_features(path: str) -> Tuple[float, float, float]:
    """返回 (white_ratio, color_ratio, luma_variance)。

    white_ratio: 近白像素占比（R,G,B 均 > 210）。
    color_ratio: 饱和/彩色像素占比（max-min > 40）。
    luma_variance: 亮度方差（内容丰富度的代理指标）。
    用于区分「白底会议室」（色彩少、方差低）与「软件界面」（色彩多、方差高）。
    """
    img = Image.open(path).convert("RGB").resize((320, 180))
    px = list(img.getdata())
    n = len(px)
    if n == 0:
        return (0.0, 0.0, 0.0)
    white = sum(1 for p in px if p[0] > 210 and p[1] > 210 and p[2] > 210) / n
    color = sum(1 for p in px if max(p) - min(p) > 40) / n
    lum = [0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] for p in px]
    m = sum(lum) / n
    var = sum((x - m) ** 2 for x in lum) / n
    return (white, color, var)


def _extract_frames(source: str, fps: float, start: float, duration: float,
                     tmp: str, scale_w: int = 320) -> List[str]:
    """抽帧到 tmp（缩小到 scale_w 宽以省磁盘/提速），返回排序后的路径列表。

    start/duration 单位秒；-ss 置于 -i 前做快速定位。
    """
    pattern = os.path.join(tmp, "f%03d.png")
    vf = f"fps={fps},scale={scale_w}:-1"
    run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", source,
         "-vf", vf, "-t", f"{duration:.3f}", "-q:v", "4", pattern])
    return sorted(glob.glob(os.path.join(tmp, "f*.png")))


def detect_intro(source: str, sample_fps: float = 10.0,
                 max_scan: float = 240.0, blue_thr: float = 30.0,
                 min_intro: float = 0.05, intro_max_seconds: float = 60.0,
                 meeting_step: float = 1.0, meeting_scan: float = 20.0,
                 ) -> float:
    """返回需要删除的开场时长（秒）；0 表示没有腾讯会议开场（忽略）。

    核心约定（防止误删真实内容）：
      开场「只在视频开头有」。只删除开头部分；若开头既无蓝屏也无腾讯会议
      界面文字，直接返回 0（忽略，不裁剪任何内容）。绝不在无开场的视频上乱删。

    流程：
      1) 高频（15fps）扫描开头 ~3s，捕获极短蓝屏 + 判定第一帧是否腾讯会议界面：
         第一帧须蓝色 **或** OCR 命中腾讯会议专属文字（腾讯会议/会议号/快速会议/
         创建者…），否则不是会议开场，直接返回 0。
      2) 定位最后连续蓝帧 → blue_end（蓝屏本身极短，常仅 1 帧）。
      3) 【2026-08-21 新增】等候室文字检测：从蓝屏结束处起按 meeting_step 抽帧
         OCR，找**连续命中腾讯会议文字段的末尾**（白底等候室/会议进行中界面），
         定位到真实内容（软件界面）开始前的最后时刻。这解决了旧版只认蓝屏、
         白底等候室（无蓝屏）开场漏裁的问题（实测视频② 等候室 ~0.8s 曾被保留）。
      4) 仅向前探测「开场窗口」(blue_end + intro_max_seconds) 寻找
         会议室→软件界面的转折；窗口之外不再扫描。找到则 meeting_end=转折处；
         找不到则 meeting_end=blue_end（只删蓝屏，不误删等候室之后的真实内容）。
      5) 综合取 文字检测末尾 / 转折点 中更晚者（更贴近真实内容起点），
         返回 min(meeting_end, blue_end + 窗口)，保证绝不超出开场窗口。

    蓝屏极短（本素材仅 t=0.0 一帧），必须用高频采样才能命中；会议室段
    用低频 + 缩小抽帧（scale）以省时省盘。
    """
    dur = get_duration(source)
    if dur <= 0:
        return 0.0

    tmp = tempfile.mkdtemp(prefix="intro_")
    try:
        # ---- 阶段 A：高频前缀抓蓝屏 + 第一帧腾讯会议文字判定 ----
        prefix_dur = min(3.0, dur)
        prefix = _extract_frames(source, 15.0, 0.0, prefix_dur, tmp)
        if not prefix:
            return 0.0
        first_blue = _frame_is_blue(prefix[0], blue_thr)
        try:
            first_meeting = _frame_is_meeting_text(prefix[0])
        except Exception:  # noqa
            first_meeting = False
        if not first_blue and not first_meeting:
            return 0.0

        last_blue_idx = 0
        for k, fp in enumerate(prefix):
            if _frame_is_blue(fp, blue_thr):
                last_blue_idx = k
            else:
                break
        # 前缀 fps=15 → 帧 k 对应时刻 k/15
        blue_end = (last_blue_idx + 1) / 15.0
        if first_blue and blue_end < min_intro:
            return 0.0

        # ---- 阶段 A2【新增】：等候室/会议进行中 文字检测 ----
        # 白底腾讯会议界面（无蓝屏或蓝屏后）→ 用 OCR 定位「真实内容开始前
        # 最后一帧会议界面」。从视频 0 起细步长扫描（蓝屏帧无文字不命中，
        # 等候室帧命中，软件界面无文字 → 断）。这解决了旧版只认蓝屏、
        # 白底等候室开场漏裁的问题（实测视频② 等候室 ~0.7s 曾被保留）。
        meeting_text_end = 0.0
        if first_meeting or first_blue:
            scan_lim = min(dur, float(meeting_scan))
            t = 0.0
            last_hit = 0.0
            hit_any = False
            while t <= scan_lim + 1e-6:
                fp = os.path.join(tmp, "mt_%04d.png" % int(round(t * 10)))
                if si._extract_frame(source, t, fp, width=1280):
                    if _frame_is_meeting_text(fp):
                        hit_any = True
                        last_hit = t
                    elif hit_any:
                        # 连续命中段已结束（会议界面切走）→ 停止
                        break
                t += float(meeting_step)
            if hit_any:
                meeting_text_end = min(last_hit + 0.4, float(meeting_scan))

        # ---- 阶段 B：仅向前探测「开场窗口」寻找 会议室→软件 转折 ----
        # 关键：开场最多只删 blue_end + 窗口 秒，绝不向后扫描到真实内容
        # (可能数分钟之后)造成误删。
        window = float(intro_max_seconds)
        body_start = blue_end
        body_dur = min(max_scan, dur) - body_start
        body_dur = min(body_dur, window)          # 只探测开场窗口
        meeting_end = blue_end
        if body_dur > 0:
            body = _extract_frames(source, 3.0, body_start, body_dur, tmp)
            if body:
                # 会议室基线（body 开头 ~1.5s）
                base_frames = body[:max(1, int(round(1.5 * 3.0)))]
                feats = [_frame_features(f) for f in base_frames]
                base_color = statistics.median(c for _, c, _ in feats)
                base_var = statistics.median(v for _, _, v in feats)

                # 转折阈值：相对基线跳变（带绝对保底下限，防止基线极低时失效）。
                color_thr = max(base_color * 1.25, base_color + 0.02)
                var_thr = max(base_var * 1.10, base_var + 300.0)

                # 持续 ≥ 1.5s 的跳变才认定为软件界面开始（body fps=3 → 5 帧）
                need = max(1, int(round(1.5 * 3.0)))
                sustained = 0
                for k in range(len(body)):
                    _, c, v = _frame_features(body[k])
                    if c >= color_thr and v >= var_thr:
                        sustained += 1
                        if sustained >= need:
                            meeting_end = body_start + (k - need + 1) / 3.0
                            break
                    else:
                        sustained = 0

        # ---- 综合：文字检测更直接，优先采用；再夹进开场窗口 ----
        if meeting_text_end > meeting_end:
            meeting_end = meeting_text_end
        return float(min(meeting_end, blue_end + window))
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def trim_intro(source: str, out_path: str, intro_end: float,
               crf: int = 20, preset: str = "veryfast") -> str:
    """重新编码去掉 [0, intro_end] 的开场，输出到 out_path。"""
    run([
        "ffmpeg", "-y", "-ss", f"{intro_end:.3f}", "-i", source,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", out_path,
    ])
    return out_path


def _in_ranges(ts: float, ranges) -> bool:
    """判断 ts 是否落在任一 (start,end) 区间内。"""
    if not ranges:
        return False
    return any(s - 1e-6 <= ts <= e + 1e-6 for s, e in ranges)


def pick_product_page_frame(source: str, after_ts: float,
                             duration: Optional[float] = None,
                             n_samples: int = 16, blue_thr: float = 30.0,
                             exclude_ranges=None) -> float:
    """从 after_ts 之后的「正片」里挑一帧「产品页面 / 软件界面」作封面底图。

    评分偏好：内容多(亮度方差高) + 有色彩 + 非大白底 + 非蓝屏。
    这样能避开开头的白底等候室 / 蓝屏，也避开纯白 PPT 页，挑出更像
    「软件产品页」的画面当封面。

    exclude_ranges：跳过这些时间段(如已删除的企业微信段)，避免把敏感
    画面当成封面。返回所选帧的时间戳(秒)。
    """
    dur = duration if duration is not None else get_duration(source)
    if dur <= 0 or after_ts >= dur - 0.3:
        return max(after_ts - 0.5, 0.0)

    tmp = tempfile.mkdtemp(prefix="coverpick_")
    try:
        best_ts: Optional[float] = None
        best_score = -1.0
        n = max(1, int(n_samples))
        for i in range(n):
            ts = after_ts + (dur - after_ts) * (i + 0.5) / n
            if _in_ranges(ts, exclude_ranges):
                continue
            fp = os.path.join(tmp, "p%05d.png" % int(ts * 10))
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "%.3f" % ts, "-i", source,
                 "-frames:v", "1", "-q:v", "4", "-vf", "scale=320:-1", fp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not os.path.exists(fp):
                continue
            white, color, var = _frame_features(fp)
            if _frame_is_blue(fp, blue_thr):
                os.remove(fp)
                continue
            # 产品页：彩色软件界面，非纯白等候室；分数越高越「产品感」
            score = var * (1.0 - white) * (0.4 + color)
            if score > best_score:
                best_score, best_ts = score, ts
            os.remove(fp)
        return best_ts if best_ts is not None else after_ts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
