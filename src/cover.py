"""视频封面生成：16:9 横屏 + 标题文字直接叠在软件页面上。

参考效果：大号渐变描边标题浮在软件界面中央偏上位置，
半透明渐变 pill 背景保证可读性，整体保持原始画面比例。

输出 16:9 横屏（默认 1920x1080），适配 B站/抖音横屏/视频号横版。
封面帧可通过 ffmpeg 插入视频开头作为片头。
"""
from __future__ import annotations

import math
import textwrap
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont


# ------------------------------------------------------------------
# 风格预设（文字颜色 + 描边 + 背景pill）
# ------------------------------------------------------------------
STYLE_PRESETS = {
    "purple": {
        "name": "紫蓝渐变",
        # 文字本体：白→浅蓝 渐变
        "text_color": (255, 255, 255),
        "stroke_color": (120, 80, 220),      # 紫色描边
        "stroke_width": 4,
        # 背景 pill：半透明紫蓝渐变
        "pill_start": (100, 60, 180, 180),
        "pill_end":   (40, 80, 200, 150),
        # 阴影
        "shadow_color": (0, 0, 0, 160),
    },
    "red": {
        "name": "红橙渐变",
        "text_color": (255, 255, 255),
        "stroke_color": (220, 80, 50),
        "stroke_width": 4,
        "pill_start": (200, 50, 50, 180),
        "pill_end":   (220, 120, 30, 150),
        "shadow_color": (0, 0, 0, 160),
    },
    "green": {
        "name": "青绿渐变",
        "text_color": (255, 255, 255),
        "stroke_color": (40, 180, 120),
        "stroke_width": 4,
        "pill_start": (20, 150, 100, 180),
        "pill_end":   (40, 180, 160, 150),
        "shadow_color": (0, 0, 0, 160),
    },
    "dark": {
        "name": "深色半透明",
        "text_color": (255, 255, 255),
        "stroke_color": (80, 80, 100),
        "stroke_width": 3,
        "pill_start": (20, 20, 30, 190),
        "pill_end":   (40, 40, 60, 170),
        "shadow_color": (0, 0, 0, 140),
    },
    "gold": {
        "name": "金橙醒目",
        "text_color": (255, 250, 240),
        "stroke_color": (220, 160, 40),
        "stroke_width": 5,
        "pill_start": (180, 120, 20, 185),
        "pill_end":   (220, 80, 20, 155),
        "shadow_color": (0, 0, 0, 170),
    },
}


def _crop_black_borders(img: Image.Image, thr: int = 20,
                        min_ratio: float = 0.55) -> Image.Image:
    """裁掉画面四周的近黑边框（视频信箱/柱形黑边）。

    按列/行平均灰度从四边向内扫描，裁掉亮度低于阈值 thr 的连续边缘。
    若裁掉后面积过小（说明整图偏暗，可能误判），则保留原图不做裁切。
    返回裁剪后的新 Image。
    """
    g = img.convert("L")
    w, h = g.size
    px = g.load()

    def avg_col(x: int) -> float:
        step = max(1, h // 200)
        s, n = 0, 0
        for y in range(0, h, step):
            s += px[x, y]
            n += 1
        return s / max(n, 1)

    def avg_row(y: int) -> float:
        step = max(1, w // 200)
        s, n = 0, 0
        for x in range(0, w, step):
            s += px[x, y]
            n += 1
        return s / max(n, 1)

    left, right = 0, w
    while left < w and avg_col(left) < thr:
        left += 1
    while right > left and avg_col(right - 1) < thr:
        right -= 1
    top, bottom = 0, h
    while top < h and avg_row(top) < thr:
        top += 1
    while bottom > top and avg_row(bottom - 1) < thr:
        bottom -= 1

    cw, ch = right - left, bottom - top
    if cw <= 0 or ch <= 0:
        return img
    # 防止误裁：保留面积需 >= min_ratio * 原面积
    if (cw * ch) < (w * h * min_ratio):
        return img
    return img.crop((left, top, right, bottom))


def _load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    """加载字体；优先粗体。"""
    if bold:
        candidates = [
            "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑 Bold
            "C:/Windows/Fonts/simhei.ttf",      # 黑体
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_gradient_pill(overlay: Image.Image,
                        xy: Tuple[int, int, int, int],
                        color_start: Tuple[int, ...],
                        color_end: Tuple[int, ...],
                        radius: int = 24):
    """在 overlay 上画垂直渐变的圆角 pill 背景。"""
    draw = ImageDraw.Draw(overlay)
    x1, y1, x2, y2 = xy
    h = y2 - y1
    if h <= 0:
        return
    for i in range(h):
        t = i / max(h - 1, 1)
        cr = int(color_start[0] + (color_end[0] - color_start[0]) * t)
        cg = int(color_start[1] + (color_end[1] - color_start[1]) * t)
        cb = int(color_start[2] + (color_end[2] - color_start[2]) * t)
        ca = int(color_start[3] + (color_end[3] - color_start[3]) * t)
        draw.line([(x1 + radius, y1 + i), (x2 - radius, y1 + i)],
                   fill=(cr, cg, cb, ca))
    # 左右圆角帽
    for yy in range(y1, y2):
        for xx, rx in [(x1 + radius, -radius), (x2 - radius, radius)]:
            dy = yy - (y1 + y2) // 2
            if abs(dy) <= radius:
                dx = int(math.sqrt(max(radius * radius - dy * dy, 0)))
                px = rx + dx if rx > 0 else rx - dx
                if x1 <= xx + px <= x2:
                    t = (yy - y1) / max(h - 1, 1)
                    ccr = int(color_start[0] + (color_end[0] - color_start[0]) * t)
                    ccg = int(color_start[1] + (color_end[1] - color_start[1]) * t)
                    ccb = int(color_start[2] + (color_end[2] - color_start[2]) * t)
                    cca = int(color_start[3] + (color_end[3] - color_start[3]) * t)
                    try:
                        draw.point((xx + px, yy), fill=(ccr, ccg, ccb, cca))
                    except Exception:
                        pass


def _draw_stroke_text(draw: ImageDraw.Draw, xy: Tuple[int, int],
                      text: str, font: ImageFont.ImageFont,
                      fill: Tuple[int, ...],
                      stroke_fill: Tuple[int, ...],
                      stroke_width: int = 3,
                      anchor: str = "mm"):
    """绘制带描边的文字（先画描边层，再画本体）。"""
    cx, cy = xy
    sw = stroke_width
    # 描边（8 方向偏移）
    if sw > 0:
        for dx in range(-sw, sw + 1):
            for dy in range(-sw, sw + 1):
                if dx == 0 and dy == 0:
                    continue
                if dx * dx + dy * dy <= sw * sw:
                    draw.text((cx + dx, cy + dy), text,
                              font=font, fill=stroke_fill,
                              anchor=anchor, align="center")
    # 本体
    draw.text((cx, cy), text, font=font, fill=fill,
              anchor=anchor, align="center")


def generate_cover(frame_path: str, title: str, out_path: str,
                  width: int = 1920, height: int = 1080,
                  style: str = "purple",
                  font_size: int = 110,
                  title_position: float = 0.5,
                  bg_darken: float = 0.20,
                  cover_duration: float = 3.0,
                  crop_black: bool = True) -> str:
    """读取无字幕帧，生成 16:9 横屏封面（标题直接叠在画面中央）。

    布局策略：
      - 先裁掉画面四周的近黑边框（视频信箱/柱形黑边）
      - cover-fit 到 16:9 画布（放大铺满，不留黑边）
      - 大号加粗标题文字 + 半透明渐变 pill 背景，浮在画面中央
      - 整体轻微压暗，突出文字

    Args:
        frame_path:     原始视频抽帧图片路径（无烧录字幕）。
        title:          封面标题文本。
        out_path:       输出 PNG 路径。
        width/height:   输出尺寸（默认 1920x1080 即 16:9）。
        style:          风格预设名（purple/red/green/dark/gold）。
        font_size:      标题字号（默认 110，更大更醒目）。
        title_position: 标题纵向位置比例（0~1，0.5=画面正中）。
        bg_darken:      背景压暗系数（0~1）。
        cover_duration: 封面作为片头时的持续秒数（供 pipeline 用）。
        crop_black:     是否裁掉四周近黑边框。
    """
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["purple"])

    # ---- 1) 加载原始帧 ----
    raw = Image.open(frame_path).convert("RGB")
    iw, ih = raw.size

    # ---- 1.5) 裁掉四周近黑边框 ----
    if crop_black:
        raw = _crop_black_borders(raw, thr=20)
        iw, ih = raw.size

    # ---- 2) cover-fit 到 16:9 画布（放大铺满，不留黑边）----
    canvas = Image.new("RGB", (width, height), (18, 20, 28))
    scale = max(width / iw, height / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    resized = raw.resize((nw, nh), Image.LANCZOS)
    ox = (nw - width) // 2
    oy = (nh - height) // 2
    canvas.paste(resized, (ox, oy))

    # 轻微压暗
    if bg_darken > 0:
        darken = Image.new("RGB", (width, height), (0, 0, 0))
        canvas = Image.blend(canvas, darken, bg_darken)

    # ---- 3) 标题文字处理 ----
    title = (title or "").strip()
    if not title:
        title = "AI 智能视频剪辑"

    font = _load_font(font_size, bold=True)

    # 自动换行 + 自适应字号：标题最多 3 行（30 字以内都能放下且不挤），
    # 行数超限时按 0.88 比例缩小字号重排，直到 ≤3 行或字号到下限。
    MAX_LINES = 3
    MIN_FONT = 64
    while True:
        max_chars = max(6, int(width // (font_size * 1.8)))
        lines = textwrap.wrap(title, width=max_chars) or [title]
        if len(lines) <= MAX_LINES or font_size <= MIN_FONT:
            break
        font_size = int(font_size * 0.88)
        font = _load_font(font_size, bold=True)

    # 测量每行尺寸
    draw_tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_metrics = []
    max_line_w = 0
    for line in lines:
        bbox = draw_tmp.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        line_metrics.append((lw, lh))
        max_line_w = max(max_line_w, lw)

    line_gap = int(font_size * 0.30)
    padding_h = int(font_size * 0.90)
    padding_v = int(font_size * 0.50)
    pill_radius = int(font_size * 0.35)
    total_text_h = sum(lh for _, lh in line_metrics) + line_gap * (len(lines) - 1)

    # pill 尺寸与位置
    pill_w = min(max_line_w + padding_h * 2, int(width * 0.88))
    pill_h = total_text_h + padding_v * 2
    pill_x = (width - pill_w) // 2
    pill_y = int(height * title_position) - pill_h // 2

    # ---- 4) 绘制半透明渐变 pill 背景 ----
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_gradient_pill(
        overlay,
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        preset["pill_start"], preset["pill_end"],
        radius=pill_radius,
    )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    # ---- 5) 绘制带描边的标题文字 ----
    draw = ImageDraw.Draw(canvas)
    shadow_offset = int(font_size * 0.05)
    # 强化标题：描边至少 6px，确保大字在复杂画面上清晰
    stroke_w = max(preset.get("stroke_width", 4), 6)
    stroke_col = preset.get("stroke_color", (100, 100, 140))

    text_y = pill_y + padding_v + (pill_h - padding_v * 2 - total_text_h) // 2
    for line, (lw, lh) in zip(lines, line_metrics):
        cx = width // 2
        cy = text_y + lh // 2
        # 阴影
        draw.text((cx + shadow_offset, cy + shadow_offset), line,
                  font=font, fill=preset["shadow_color"],
                  anchor="mm", align="center")
        # 描边 + 本体文字
        _draw_stroke_text(
            draw, (cx, cy), line, font,
            fill=preset["text_color"],
            stroke_fill=stroke_col,
            stroke_width=stroke_w,
            anchor="mm",
        )
        text_y += lh + line_gap

    canvas.save(out_path)
    return out_path
