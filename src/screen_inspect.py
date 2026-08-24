"""高风险画面巡检（v3：左侧导航栏主特征 + 自动删除）

设计原则：
- 不依赖 OCR / 视觉 LLM（离线环境无 tesseract/pytesseract/LLM API）
- 不全片逐帧分析（避免长视频过慢）
- 两级检测：低频抽帧(每10s)预筛 → 疑似区加密抽帧(±8s,步长0.5s)确认
- **核心特征：企业微信左侧导航栏**（消息/邮件/文档/日程/待办/会议 竖排图标+文字）
  位于画面最左侧窄条(~0-10%宽度)，含 5~7 个垂直堆叠的导航项。
  此特征对好生意等 SaaS 界面具有强区分度（好生意无此导航栏布局）。
- 高置信(conf≥conf_delete)→直接写 delete_segments（自动删除）
- 中置信(conf≥conf_review)→写入 review_items（人工兜底）
- plan.json 记录 start/end/type/reason/confidence
- 在 render 前运行，不修改已验证的 ASR/字幕/编码逻辑

视觉特征：
- left_nav_bar：最左侧(0-10%)导航栏检测（竖排图标+文字项数/均匀性/饱和度）
- center_circles：中间区域圆形头像数（联系人列表辅助信号）
- blue_accent：蓝色调（企业微信蓝主题辅助确认）
- chat_light/uniformity：右侧聊天区亮度（辅助）
- rowvar：行方差（列表型UI辅助）
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 配置默认值（可被 config.py 覆盖）
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 低频采样
    "inspect_low_freq_step": 3.0,        # 全片每 N 秒抽一帧（提速 2026-08-20：企微界面连续展示，3s 一帧召回足够；32min 视频 → 644 帧 ~55min，旧 1.0s=1932 帧 ~3h+）
    # 加密采样
    "inspect_dense_window": 8.0,         # 疑似点前后各 N 秒
    "inspect_dense_step": 0.5,          # 加密采样步长
    # 特征阈值
    "thr_center_circles": 4,            # 宽区(15-58%)圆形头像 ≥ N 个才可疑
    "thr_mid_circles": 3,               # 中区(30-50%)圆形 ≥ N 个（联系人列表主体）
    "thr_chat_light_min": 0.50,         # 聊天区最低亮度
    "thr_chat_light_max": 0.92,         # 聊天区最高亮度（排除纯白页）
    "thr_chat_uniformity": 0.06,        # 聊天区亮度方差 ≤ 此值（更均匀=更像聊天区）
    "thr_rowvar": 0.038,               # 行方差 ≥ 此值（列表型UI）
    "thr_blue_accent": 0.08,           # 蓝色比例（企业微信蓝主题）
    # 左侧导航栏检测（v4 文字+排版核心特征）
    # 注意：bands = 同时有「图标+文字纹理」证据的带数，不是任何导航栏都算
    "thr_left_nav_bands": 4,           # 至少 4 个带同时有图标+文字（企微有6-7个导航项）
    "thr_left_nav_score": 0.30,        # left_nav_bar 综合得分阈值（0~1）
    # 置信度
    "conf_delete": 0.70,                # ≥ 此值直接写 delete_segments（自动删除）
    "conf_review": 0.45,                # ≥ 此值写 review_items（人工兜底）
    # 最小段长度（秒）
    "min_segment_sec": 2.0,
    # 缩略图
    "inspect_thumb_width": 640,         # 抽取代表帧的宽度（供人工核对）
    # 段合并（v5 OCR 驱动）
    "seg_gap_sec": 2.5,                  # 相邻企微帧合并间隙（秒）
    "seg_pad_sec": 0.3,                  # 企微段向两端最终安全外扩（秒）
    "seg_refine_max": 3.0,              # 边界向两端 OCR 精修最大延伸（秒）
    "seg_click_buffer": 1.2,            # 边界外允许纳入的「非企微」过渡帧总长（秒），覆盖点开/点走动作
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin\ffmpeg.EXE"


def _get_duration(video: str) -> float:
    """用 ffprobe 获取视频时长（秒）。

    注意：本环境 subprocess 的 capture_output 管道返回 None，
    故改为重定向到临时文件再读取，兼容性最好。
    """
    probe = (shutil.which("ffprobe") or
             r"E:\workbuddy\2026-08-10-16-44-11\ffmpeg\ffmpeg-9.0-full_build\bin\ffprobe.EXE")
    fd, tmp_json = tempfile.mkstemp(suffix=".json", prefix="probe_")
    os.close(fd)
    try:
        with open(tmp_json, "w", encoding="utf-8") as out:
            subprocess.run([probe, "-v", "quiet", "-print_format", "json",
                            "-show_format", video],
                           stdout=out, stderr=subprocess.DEVNULL, timeout=30)
        with open(tmp_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        return float(d["format"]["duration"])
    finally:
        try:
            os.remove(tmp_json)
        except OSError:
            pass


def _extract_frame(video: str, t: float, out: str, width: int = 1280) -> bool:
    """抽取一帧到 out，返回是否成功。width 控制输出宽度（默认 1280，OCR 必需）。

    注意（2026-08-20 修复）：
    - 抽原帧 jpg 到临时文件 → Pillow 后置 resize 到 width（绕开 ffmpeg scale filter
      在 VFR 源中后段（≥900s）偶发失效的问题：之前抽出的"全分辨率"2160x1080 图
      会让 OCR 跑 10-13s/帧，resize 后稳态 2.5s/帧）
    - 不再 capture_output=True（Windows 上对 ffmpeg 大 stderr 极易死锁/超时）
    - 改 stderr 重定向到临时文件 + 失败时打前 200 字符
    - timeout 15s → 30s（VFR 源在中后段偶发需 10s+）
    - 默认 width 480 → 1280（防止调用方忘传）
    """
    fd, err_path = tempfile.mkstemp(suffix=".log", prefix="extract_err_")
    err_f = os.fdopen(fd, "wb")  # 包装 mkstemp 的 fd 为 file object（避免 os.close+open race，且 fd 唯一）
    tmp_jpg = out + ".tmp.jpg"
    try:
        # 抽原帧 jpg（无 scale filter，避免 VFR 偶发失效）
        proc = subprocess.run(
            [_ffmpeg(), "-y", "-ss", f"{t:.3f}", "-i", video,
             "-frames:v", "1", "-q:v", "3", tmp_jpg],
            stdout=subprocess.DEVNULL, stderr=err_f, timeout=30,
        )
        err_f.close()
        if proc.returncode != 0 or not os.path.exists(tmp_jpg) or os.path.getsize(tmp_jpg) < 512:
            try:
                with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                    msg = f.read()[:200].replace("\n", " ")
                kind = "失败" if proc.returncode != 0 else "文件异常"
                print(f"  [抽帧{kind}] t={t:.1f}s rc={proc.returncode} | {msg}", flush=True)
            except Exception:
                pass
            return False
        # Pillow 后置 resize 到 width（保证 OCR 速度稳定）
        try:
            from PIL import Image
            img = Image.open(tmp_jpg)
            if img.width != width:
                ratio = width / img.width
                img = img.resize((width, int(img.height * ratio)), Image.LANCZOS)
            ext = os.path.splitext(out)[1].lower()
            if ext in (".jpg", ".jpeg"):
                img.save(out, "JPEG", quality=85)
            else:
                img.save(out, "PNG", optimize=True)
        except Exception as e:
            print(f"  [resize 失败] t={t:.1f}s {e}", flush=True)
            return False
        return os.path.exists(out) and os.path.getsize(out) > 512
    except subprocess.TimeoutExpired:
        try:
            err_f.close()
        except Exception:
            pass
        try:
            with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                msg = f.read()[:200].replace("\n", " ")
            print(f"  [抽帧超时] t={t:.1f}s (>30s) | {msg}", flush=True)
        except Exception:
            pass
        return False
    finally:
        try:
            err_f.close()
        except Exception:
            pass
        try:
            os.remove(err_path)
        except OSError:
            pass
        try:
            os.remove(tmp_jpg)
        except OSError:
            pass


def _sample_frames(video: str, times: List[float], outdir: str, width: int = 480) -> List[Tuple[float, str]]:
    """批量抽取帧，返回 [(时间, 路径)] 列表。width 控制抽帧宽度（OCR 需较高分辨率）。"""
    results = []
    for t in times:
        name = f"f_{int(round(t * 1000))}.png"
        path = os.path.join(outdir, name)
        if _extract_frame(video, t, path, width=width):
            results.append((t, path))
    return results


# ---------------------------------------------------------------------------
# OCR 判定（v5：基于文字内容区分企业微信 vs 同类 SaaS 左栏）
# ---------------------------------------------------------------------------
_OCR = None

# 企微「强特征词」：邮件/日程/会议/文档——企微 app 导航项。
# 好生意左栏实测不含这些词（其导航为 客户中心/销售管理/采购管理…），故好生意安全；
# 「文档」虽也是 Windows 文件夹名，但 Windows 资源管理器由 WINDOWS_VETO_KEYS 否决，
# 不会误删。判定需 ≥2 强词共存（或 1 强词+企业微信专属词），单词不足以定案。
WECHAT_STRONG_KEYS = ["邮件", "日程", "会议", "文档"]
# 企微「专属应用词」：仅「企业微信」——经实测，好生意/畅捷通左栏也含
# 「企业协同」「应用中心」（它们自己的协同模块入口），故这两项绝不可作判据；
# 只有「企业微信」是企微独占词（好生意 OCR 为「畅捷通好生意」，与「企业微信」
# 编辑距离≥2，不会被模糊匹配误命中）。模糊匹配可包容 OCR 把「企业微信」认成
# 「企业协」(微/协 差1字)。
WECHAT_APP_TERMS = ["企业微信"]
# 企微左栏全部导航项（用于「多关键词」验证）。
WECHAT_NAV_KEYS = ["消息", "邮件", "文档", "日程", "待办", "会议",
                   "通讯录", "工作台", "收藏", "企业协同", "应用中心"]
# Windows 资源管理器 / 桌面「否决词」：左栏出现这些即判定为系统资源管理器，
# 绝非企微（此电脑/回收站/控制面板/网络/快速访问 为 Windows 专属）。
WINDOWS_VETO_KEYS = ["此电脑", "回收站", "控制面板", "网络", "快速访问"]


def _get_ocr():
    """懒加载 RapidOCR（首次调用时初始化，约 1~2s）。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def _ocr_wechat_texts(img_path: str) -> List[str]:
    """对一帧最左侧导航栏区域做 OCR，返回识别到的文字列表。

    精度/稳定性优化（v6）：
      - 裁取左侧 14% 宽（覆盖企微/同类 SaaS 导航栏，含三字符词如通讯录/工作台）
      - 按目标高度放大（而非固定倍率），保证小字号也足够清晰
      - 轻度对比度拉伸（直方图 2%~98% 截断），弱化抗锯齿/半透明导致的漏检
    """
    ocr = _get_ocr()
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    lw = max(int(w * 0.14), 28)
    left = img.crop((0, 0, lw, h))
    # 按目标高度放大（约 1100px），上限 4x 防止过大拖慢 det
    target_h = 1100
    scale = min(4.0, max(2.5, target_h / float(left.height)))
    nw, nh = int(left.width * scale), int(left.height * scale)
    left = left.resize((nw, nh), Image.LANCZOS)
    # 轻度对比度拉伸（CLAHE-lite）：整体对比拉伸，提升小字可读性
    arr = np.array(left.convert("RGB")).astype(np.float32)
    lum = arr.mean(axis=2)
    lo, hi = np.percentile(lum, (2, 98))
    if hi > lo:
        arr = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255)
    arr = arr.astype(np.uint8)
    try:
        result, _ = ocr(arr)
    except Exception:
        return []
    return [b[1] for b in (result or [])]


def _ocr_full_texts(img_path: str) -> List[str]:
    """对整帧（不裁切左栏）做 OCR，返回识别到的全部文字。

    用途（2026-08-21 重构）：① 在聊天区/内容区检测企微强词（微信群/交付群/客户群…）；
    ② 产品页排除——好生意产品页的「商品/库存/订单/规格/单位/价格」等词出现在中部
       内容区，左栏裁切会漏掉，必须整帧 OCR 才能拦下。
    轻度放大保证小字清晰；限制最大边 1280 避免大图拖慢 det。
    """
    ocr = _get_ocr()
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    scale = min(2.0, max(1.0, 1280.0 / float(w)))
    if scale != 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    arr = np.array(img.convert("RGB")).astype(np.float32)
    try:
        result, _ = ocr(arr)
    except Exception:
        return []
    return [b[1] for b in (result or [])]


def _ocr_full_boxes(img_path: str) -> List[Tuple[str, float]]:
    """对整帧做 OCR，返回 [(文字, 该文字中心 x 占比)]。

    相比 _ocr_full_texts 多带回文字水平中心占比（x/图宽，0~1），
    供 risk_screen 用 x<0.14 直接派生「左栏文字」，从而**一次 OCR 同时完成**
    左栏导航评分 + 整帧强词/产品页检测，避免每帧两次 OCR 的内存压力与不稳。
    """
    ocr = _get_ocr()
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    # 全帧 OCR 仅用于关键词/产品词检测，640 宽足够（UI 文字较大）；
    # 缩小输入显著降低 RapidOCR det/rec 耗时（密集产品页每帧可从 6s 降到 ~2s）。
    scale = min(1.2, max(1.0, 640.0 / float(w)))
    if scale != 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    arr = np.array(img.convert("RGB")).astype(np.float32)
    try:
        result, _ = ocr(arr)
    except Exception:
        return []
    out: List[Tuple[str, float]] = []
    if result:
        for box, text, _ in result:
            xs = [p[0] for p in box]
            xc = (min(xs) + max(xs)) / 2.0 / float(img.width)
            out.append((text, xc))
    return out


def _char_diff(a: str, b: str) -> int:
    """两串编辑距离（仅中文短词，长度差也计入）。"""
    return sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))


def _fuzzy_has(joined: str, key: str) -> bool:
    """容忍 OCR 形近/音近错字：直接包含，或在文本中滑动 N-gram 与关键词
    编辑距离 ≤1 即算命中（如「邮们」「文挡」「会仪」）。"""
    if key in joined:
        return True
    L = len(key)
    for i in range(0, len(joined) - L + 1):
        if _char_diff(joined[i:i + L], key) <= 1:
            return True
    return False


def _ocr_analyze_wechat(img_path: str) -> Dict[str, Any]:
    """分析一帧是否为企业微信左栏。

    返回: {is_wechat, texts, hit_keys, strong_hits, n_strong,
           app_terms, windows_veto}
      - app_terms ≥ 1      → 企微专属词命中（企业协同/应用中心…），最强信号
      - n_strong ≥ 2        → 企微导航多词共存（邮件/日程/会议/待办），几乎不误判
      - n_strong == 1        → 边界，需结构信号兜底（见 run()）
      - windows_veto == True → 左栏是 Windows 资源管理器（此电脑/回收站…），非企微
    """
    texts = _ocr_wechat_texts(img_path)
    joined = "".join(texts)
    hit_keys: set = set()
    for k in WECHAT_NAV_KEYS:
        if _fuzzy_has(joined, k):
            hit_keys.add(k)
    strong = [k for k in hit_keys if k in WECHAT_STRONG_KEYS]
    app_terms = [k for k in WECHAT_APP_TERMS if _fuzzy_has(joined, k)]
    windows_veto = any(_fuzzy_has(joined, k) for k in WINDOWS_VETO_KEYS)
    return {
        "is_wechat": (len(strong) >= 2) or (len(app_terms) >= 1),
        "texts": joined,
        "hit_keys": hit_keys,
        "strong_hits": strong,
        "n_strong": len(strong),
        "app_terms": app_terms,
        "windows_veto": windows_veto,
    }


def _ocr_is_wechat(img_path: str) -> Tuple[bool, str]:
    """兼容旧接口（_refine_edge 用）。边界精修时允许 ≥1 强词/专属词即视为企微，
    但 Windows 资源管理器否决（避免把桌面误并入企微段）。"""
    info = _ocr_analyze_wechat(img_path)
    return ((info["n_strong"] >= 1 or len(info["app_terms"]) >= 1)
            and not info["windows_veto"]), info["texts"]


def _refine_edge(video: str, anchor: float, direction: int,
                 cfg: Dict[str, Any], dur: float) -> float:
    """以 OCR 命中的锚点向某一端精修边界，覆盖「点开/点走企业微信」的过渡动作。

    direction = -1 向左(更早)精修；= +1 向右(更晚)精修。
    逻辑：
      1. 以 0.25s 步长从 anchor 向 direction 延伸，抽帧做 OCR；
      2. OCR 命中企业微信 → 边界继续外扩，并清零过渡帧计数；
      3. OCR 未命中（窗口载入/关闭动画、桌面/启动器画面）→ 计为过渡帧，
         只要累计过渡帧总长 ≤ seg_click_buffer，仍纳入（即包含点击动作本身）；
      4. 超过 seg_click_buffer 或达到 seg_refine_max / 视频边界 → 停止。
    返回精修后的边界时间（秒）。
    """
    step = 0.25
    max_ext = float(cfg.get("seg_refine_max", 3.0))
    click_buf = float(cfg.get("seg_click_buffer", 1.2))
    refine_dir = os.path.join(tempfile.gettempdir(), "si_refine")
    os.makedirs(refine_dir, exist_ok=True)

    t = anchor
    buf = 0.0
    while True:
        nt = round(t + direction * step, 3)
        if nt < 0 or nt > dur:
            break
        if abs(nt - anchor) > max_ext:
            break
        name = f"r_{int(round(nt * 1000))}_{direction}.png"
        path = os.path.join(refine_dir, name)
        if not _extract_frame(video, nt, path, width=1280):
            break
        is_we, _ = _ocr_is_wechat(path)
        if is_we:
            t = nt
            buf = 0.0
        else:
            buf += step
            if buf > click_buf:
                break
            t = nt
    return t


# ---------------------------------------------------------------------------
# 特征提取（PIL/numpy，无需 cv2/OCR）
# ---------------------------------------------------------------------------
import numpy as np
from PIL import Image


def _features(img_path: str) -> Dict[str, float]:
    """从一帧图像提取高风险画面特征向量。

    v3: 以「企业微信左侧导航栏」为核心主特征（替代不可靠的绿气泡）。
    左侧导航栏特征：画面最左 0~10% 宽度区域内，
    检测垂直堆叠的导航项（图标+文字），典型含 5~7 项
    （消息/邮件/文档/日程/待办/会议）。
    """
    img = Image.open(img_path).convert("RGB").resize((320, 180))
    a = np.asarray(img, dtype=np.float32) / 255.0  # h,w,3
    h, w = a.shape[:2]
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]

    # ═══ 核心特征 v3：企业微信左侧导航栏检测 ═══
    # 取画面最左侧窄条 (0~10% 宽度)，这是企业微信导航栏所在位置
    left_nav_score, left_nav_bands = _detect_left_nav_bar(a, h, w)

    # 蓝色调（企业微信蓝 #1890ff / 好生意深蓝）
    blue_mask = (b > 0.45) & (b > r + 0.10)
    blue_accent = float(np.mean(blue_mask))

    # 联系人列表圆形头像检测（扩展区域：15%~58%，覆盖三栏布局的中间列）
    x0_wide, x1_wide = int(w * 0.15), int(w * 0.58)
    mid_wide = a[:, x0_wide:x1_wide, :]
    mid_sat = np.max(mid_wide, axis=2) - np.min(mid_wide, axis=2)
    mid_sat_mask = mid_sat > 0.18
    center_circles = _count_round_blobs(mid_sat_mask)

    # 左窄区(15%-30%)圆形数 — 导航菜单图标通常在这里
    x0_left, x1_left = int(w * 0.15), int(w * 0.30)
    left_region = a[:, x0_left:x1_left, :]
    left_sat = np.max(left_region, axis=2) - np.min(left_region, axis=2)
    left_circles = _count_round_blobs((left_sat > 0.18))

    # 中间区(30%-50%)圆形数 — 联系人列表主体
    x0_mid, x1_mid = int(w * 0.30), int(w * 0.50)
    mid_center = a[:, x0_mid:x1_mid, :]
    mid_center_sat = np.max(mid_center, axis=2) - np.min(mid_center, axis=2)
    mid_circles = _count_round_blobs((mid_center_sat > 0.18))

    # 右侧聊天区(55%~100%)亮度与均匀性
    right = a[:, int(w * 0.55):, :]
    rr, rg, rb = right[:, :, 0], right[:, :, 1], right[:, :, 2]
    right_lum = (rr + rg + rb) / 3.0
    chat_light = float(np.mean(right_lum))
    chat_uniformity = float(np.std(right_lum))

    # 行亮度方差（列表型UI周期性行 vs 表格均匀网格）
    row_mean = a.reshape(h, -1).mean(axis=1)
    rowvar = float(np.var(row_mean))

    # 三栏布局检测：沿X轴计算列间亮度跳变次数
    col_profile = a.mean(axis=(0, 2))
    col_diff = np.abs(np.diff(col_profile))
    col_edges = int(np.sum(col_diff > (np.std(col_diff) * 1.5 + 0.02)))

    return {
        "left_nav_bar": left_nav_score,       # v3 核心：左侧导航栏得分(0~1)
        "left_nav_bands": float(left_nav_bands),  # 匹配导航项的水平带数量
        "blue_accent": blue_accent,
        "center_circles": center_circles,
        "left_circles": left_circles,
        "mid_circles": mid_circles,
        "chat_light": chat_light,
        "chat_uniformity": chat_uniformity,
        "rowvar": rowvar,
        "col_edges": float(col_edges),
    }


def _detect_left_nav_bar(a: np.ndarray, h: int, w: int) -> Tuple[float, int]:
    """v4 核心：基于文字+排版检测企业微信左侧导航栏。

    用户明确要求：不根据颜色判断！要根据左侧导航栏的**具体文字内容**
    （消息/邮件/文档/日程/待办/会议）和**排版结构**（图标在上+文字在下竖排）。

    企业微信 vs 好生意左栏的关键区别（同在画面最左 0~10%）：
     · 企业微信：每项 = 图标(上) + 中文文字标签(下)，文字为「消息/邮件/
       文档/日程/待办/会议」等 2 字词，共 6~7 项均匀排列
     · 好生意：左栏也是竖排导航，但文字不同（搜索客户/快捷导航/智能推
       广等），且可能只有图标无文字、或排版密度不同

    无 OCR 环境下的文字检测策略（像素级纹理特征）：
     1. 将每条水平带分为「上半(图标区)」和「下半(文字区)」
     2. 文字区检测：
        - 水平边缘密度（Sobel-x）：中文字符有密集横笔画 → 高水平边缘
        - 局部纹理方差（小窗口 std）：文字区局部变化远大于纯色背景
        - 文字行对比度：文字与背景间有明显亮度跳变
     3. 「图标+文字」组合才是企微导航项（仅有图标→可能是好生意菜单）

    返回: (score, band_count)
      score ∈ [0, 1] — 越高越像企业微信导航栏
      band_count — 同时满足「图标+文字」特征的带数
    """
    left_w = max(int(w * 0.10), 8)
    strip = a[:, :left_w, :,]          # h, left_w, 3
    gray = np.mean(strip, axis=2)       # h, left_w  灰度图

    n_bands = 8
    band_h = max(h // n_bands, 6)       # 每带高度（需≥6 才能分上下两半）

    matched_bands = 0          # 同时有图标+文字的带数
    icon_only_bands = 0        # 只有图标没有文字的带数
    text_only_bands = 0        # 只有文字没有图标的带数
    band_scores = []
    text_densities = []        # 收集所有带的文字密度，用于一致性检查

    for i in range(n_bands):
        y0 = i * band_h
        y1 = min((i + 1) * band_h, h)
        if y1 - y0 < 4:
            band_scores.append(0.0); continue

        band = strip[y0:y1, :, :]
        band_gray = gray[y0:y1]

        # ---- 分上下半：上=图标区，下=文字区 ----
        mid = (y1 - y0) // 2
        if mid < 2:
            upper = band; lower = band
            upper_g = band_gray; lower_g = band_gray
        else:
            upper = band[:mid, :, :]; lower = band[mid:, :, :]
            upper_g = band_gray[:mid]; lower_g = band_gray[mid:]

        # === 图标区特征 ===
        sat_u = np.max(upper, axis=2) - np.min(upper, axis=2)
        icon_sat_mean = float(np.mean(sat_u))
        icon_high_sat = float(np.mean(sat_u > 0.20))  # 彩色图标像素占比
        icon_has_content = icon_sat_mean > 0.04 or icon_high_sat > 0.01

        # === 文字区特征（核心！）===
        # 特征1：水平边缘密度（Sobel-x）— 中文字符横笔画多 → 高水平边缘
        if lower.shape[0] >= 2 and lower.shape[1] >= 2:
            # 手动 Sobel-x（避免 scipy 依赖）
            gx = np.abs(lower_g[:, 2:] - lower_g[:, :-2]) if lower.shape[1] > 2 \
                 else np.zeros_like(lower_g)
            h_edge_density = float(np.mean(gx)) if gx.size > 0 else 0.0
        else:
            h_edge_density = 0.0

        # 特征2：局部纹理方差（滑动窗口标准差）— 文字区局部变化大
        if lower.size >= 9:
            # 用简单差分近似局部方差
            dx = np.abs(lower_g[:, 1:] - lower_g[:, :-1]).mean() if lower.shape[1] > 1 else 0
            dy = np.abs(lower_g[1:, :] - lower_g[:-1, :]).mean() if lower.shape[0] > 1 else 0
            local_texture = float((dx + dy) / 2.0)
        else:
            local_texture = 0.0

        # 特征3：文字区整体对比度（max-min luminance）
        text_contrast = float(np.max(lower_g) - np.min(lower_g)) if lower.size > 0 else 0.0

        # 特征4：文字区饱和度变化（文字本身低饱和但有轮廓）
        sat_l = np.max(lower, axis=2) - np.min(lower, axis=2)
        text_sat_var = float(np.std(sat_l))  # 饱和度方差（文字轮廓造成的变化）

        # ---- 综合判定此带是否为「企微导航项（图标+文字）」----
        score = 0.0
        has_icon = False
        has_text = False

        # 图标区打分
        icon_score = 0.0
        if icon_has_content:
            icon_score += 0.30
        if icon_high_sat > 0.008:
            icon_score += 0.20          # 有彩色图标
        if icon_score >= 0.25:
            has_icon = True

        # 文字区打分（v4 核心——必须通过文字检测才可能命中）
        text_score = 0.0
        # 条件T1：水平边缘密度够高（中文字符的横笔画特征）
        if h_edge_density > 0.025:
            text_score += 0.30
        elif h_edge_density > 0.015:
            text_score += 0.15
        # 条件T2：局部纹理复杂度够高（文字不是纯色）
        if local_texture > 0.035:
            text_score += 0.25
        elif local_texture > 0.020:
            text_score += 0.12
        # 条件T3：对比度够（文字和背景可区分）
        if text_contrast > 0.12:
            text_score += 0.20
        elif text_contrast > 0.06:
            text_score += 0.08
        # 条件T4：饱和度方差（文字轮廓造成的细微变化）
        if text_sat_var > 0.02:
            text_score += 0.15
        elif text_sat_var > 0.01:
            text_score += 0.05

        has_text = text_score >= 0.40  # 文字区阈值

        # 组合判定
        if has_icon and has_text:
            # ★ 完美匹配：图标+文字都有 → 强信号（企微导航项）
            score = 0.50 + min(icon_score, 0.40) * 0.3 + min(text_score, 0.80) * 0.3
            matched_bands += 1
        elif has_text and not has_icon:
            # 有文字但无明显图标 → 可能是其他 UI 的文字标签
            score = text_score * 0.5
            text_only_bands += 1
        elif has_icon and not has_text:
            # 有图标但无文字 → 可能是好生意纯图标菜单
            score = icon_score * 0.4
            icon_only_bands += 1
        else:
            score = 0.0

        band_scores.append(score)
        if has_text:
            text_densities.append(h_edge_density)

    # ---- 全局一致性加分 ----
    # 企微导航栏的文字密度在各带之间应该比较一致（都是2个中文字）
    consistency_bonus = 0.0
    if len(text_densities) >= 3:
        td = np.array(text_densities)
        cv = float(np.std(td) / (np.mean(td) + 1e-6))  # 变异系数
        if cv < 0.35:                    # 各带文字密度一致
            consistency_bonus = 0.10
        elif cv < 0.55:
            consistency_bonus = 0.05

    # ---- 排除好生意：如果大部分带是「图标-only」而非「图标+文字」→ 降权 ----
    total_active = matched_bands + icon_only_bands + text_only_bands
    if total_active >= 3 and icon_only_bands > matched_bands:
        # 好生意模式：很多带只有图标没有文字
        consistency_bonus -= 0.15

    # ---- 综合得分 ----
    band_ratio = matched_bands / n_bands
    avg_score = sum(band_scores) / len(band_scores) if band_scores else 0
    final_score = band_ratio * 0.50 + avg_score * 0.35 + consistency_bonus
    final_score = max(0.0, min(final_score, 1.0))

    return final_score, matched_bands


def _count_round_blobs(mask: np.ndarray) -> int:
    """在布尔掩码上做连通分量，统计近似圆形的中等大小团块数量。
    用于检测联系人列表中的圆形头像。"""
    m = mask.astype(np.uint8)
    seen = np.zeros_like(m)
    bh, bw = m.shape
    count = 0
    for y in range(bh):
        for x in range(bw):
            if m[y, x] and not seen[y, x]:
                comp = []
                stack = [(y, x)]
                seen[y, x] = 1
                while stack:
                    cy, cx = stack.pop()
                    comp.append((cy, cx))
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < bh and 0 <= nx < bw and m[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = 1
                            stack.append((ny, nx))
                sz = len(comp)
                if sz >= 10:  # 必须有一定大小
                    ys = [p[0] for p in comp]; xs = [p[1] for p in comp]
                    bh_sz = max(ys) - min(ys) + 1
                    bw_sz = max(xs) - min(xs) + 1
                    if bh_sz > 0 and 0.6 <= bw_sz / bh_sz <= 1.7 and 10 <= sz <= 500:
                        count += 1
    return count


# ---------------------------------------------------------------------------
# 判定逻辑
# ---------------------------------------------------------------------------
def _score(feat: Dict[str, float], cfg: Dict[str, Any]) -> Tuple[bool, float, str]:
    """返回 (is_suspect, confidence, reason)。

    v4 判定策略（文字+排版为核心，不依赖颜色）：
      - ★ 主信号：left_nav_bar（基于文字纹理的导航栏检测）
        → 必须同时满足 score 阈值 AND bands(图标+文字匹配数) 阈值
        → bands 代表「同时有图标+文字特征」的带数，是核心证据
      - 辅助信号：联系人圆形 + 聊天区 + 蓝主题 + 多栏布局
      - 抑制：好生意模式（icon-only 带多 + 无文字）
    """
    signals = []
    conf = 0.0

    # ═══ ★★★ 主信号：企业微信左侧导航栏（v4 文字+排版）═══
    lnv = feat.get("left_nav_bar", 0.0)
    lnb = feat.get("left_nav_bands", 0)   # 图标+文字同时命中的带数
    thr_lnv = cfg.get("thr_left_nav_score", DEFAULTS.get("thr_left_nav_score", 0.35))
    thr_lnb = cfg.get("thr_left_nav_bands", DEFAULTS.get("thr_left_nav_bands", 4))

    # v4 关键：必须同时满足 score 和 bands（文字证据），不能只靠结构
    if lnv >= thr_lnv and lnb >= thr_lnb:
        signals.append(f"企微导航栏-文字确认(score={lnv:.2f}, 图文匹配带={int(lnb)})")
        conf += 0.45                    # 主信号权重
        if lnb >= 6:                    # 6+ 带都有文字 → 非常确定
            conf += 0.15
        elif lnb >= 5:
            conf += 0.08
    elif lnv >= thr_lnv * 0.7 and lnb >= max(thr_lnb - 1, 2):
        # 弱信号但仍有部分文字证据
        signals.append(f"弱导航栏-部分文字(score={lnv:.2f}, 带={int(lnb)})")
        conf += 0.15
    # 注意：如果只有 score 但 bands=0（纯结构/无文字）→ 不触发主信号！

    # 信号 A：中间宽区圆形头像多（联系人列表）— 辅助确认
    if feat["center_circles"] >= cfg.get("thr_center_circles", DEFAULTS["thr_center_circles"]):
        signals.append(f"联系人列表({feat['center_circles']}个圆形)")
        conf += 0.15

    # 信号 A2：中区(30-50%)有圆形 — 更精确的联系人列表信号
    if feat["mid_circles"] >= cfg.get("thr_mid_circles", DEFAULTS.get("thr_mid_circles", 3)):
        signals.append(f"中区联系人({feat['mid_circles']}个圆)")
        conf += 0.15

    # 信号 B：聊天区亮且均匀（右侧是聊天/内容区）
    cl = cfg.get("thr_chat_light_min", DEFAULTS["thr_chat_light_min"])
    cu = cfg.get("thr_chat_uniformity", DEFAULTS["thr_chat_uniformity"])
    if cl <= feat["chat_light"] <= cfg.get("thr_chat_light_max", DEFAULTS["thr_chat_light_max"]) \
       and feat["chat_uniformity"] <= cu:
        signals.append(f"聊天区(亮={feat['chat_light']:.2f},匀={feat['chat_uniformity']:.3f})")
        conf += 0.12

    # 信号 C：行方差高（列表型UI）
    if feat["rowvar"] >= cfg.get("thr_rowvar", DEFAULTS["thr_rowvar"]):
        signals.append(f"列表行结构(var={feat['rowvar']:.3f})")
        conf += 0.08

    # 信号 E：企业微信蓝色主题
    if feat["blue_accent"] >= cfg.get("thr_blue_accent", DEFAULTS.get("thr_blue_accent", 0.08)):
        signals.append(f"企业微信蓝({feat['blue_accent']:.3f})")
        conf += 0.15

    # 信号 F：三栏布局
    if 2 <= feat["col_edges"] <= 6:
        signals.append(f"多栏布局(edges={int(feat['col_edges'])})")
        conf += 0.08

    # ═══ 抑制规则 ═══
    # 如果没有导航栏信号但有大量圆形头像+暗区 → 可能是好生意/其他 SaaS 导航页
    if lnv < thr_lnv * 0.5 and feat["mid_circles"] >= 4 \
       and feat["chat_uniformity"] > 0.15 and feat["chat_light"] < 0.88:
        conf -= 0.35
        signals.append("[降权:无导航栏但疑似其他SaaS界面]")

    # 组合判定
    is_suspect = conf >= cfg.get("conf_review", DEFAULTS["conf_review"])
    reason = "; ".join(signals) if signals else "无明显风险特征"

    # cap at 1.0; floor at 0
    conf = max(0.0, min(conf, 1.0))
    return is_suspect, conf, reason


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(
    video: str,
    plan_path: str,
    workdir: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """执行高风险画面巡检（v3：左侧导航栏主特征 + 自动删除）。

    两级检测流程：
      1. 低频抽样（每 inspect_low_freq_step 秒）→ 对每帧计算特征 → 标记疑似帧
      2. 对疑似帧聚类 → 每簇前后 ±inspect_dense_window 秒加密抽样 → 重算特征确认连续区间
      3. 高置信(conf≥conf_delete) → delete_segments（自动删除）
         中置信(conf≥conf_review) → review_items（人工兜底）
      4. 合并写入 plan.json（追加，不覆盖已有项）

    v3 核心：以「企业微信左侧导航栏」为主检测特征，
    替代 v2 中不可靠的绿气泡信号。
    """
    c = {**DEFAULTS, **(cfg or {})}
    dur = _get_duration(video)
    tmp = os.path.join(workdir or tempfile.mkdtemp(prefix="si_"), "_screen_inspect")
    os.makedirs(tmp, exist_ok=True)

    result = {
        "video": video,
        "plan": plan_path,
        "duration": dur,
        "low_freq_frames": 0,
        "suspect_count": 0,
        "dense_frames": 0,
        "delete_segments": [],
        "review_items": [],
        "status": "ok",
    }

    # ---- 阶段 1：低频抽样 ----
    low_times = list(float(i) * c["inspect_low_freq_step"]
                     for i in range(int(dur // c["inspect_low_freq_step"]) + 1))
    if low_times[-1] < dur - 0.05:
        low_times.append(dur - 0.05)
    low_dir = os.path.join(tmp, "low")
    os.makedirs(low_dir, exist_ok=True)
    low_samples = _sample_frames(video, low_times, low_dir, width=1280)
    result["low_freq_frames"] = len(low_samples)

    # ---- 阶段 2：OCR 判定（v6 核心：专属词 + 多关键词 + 结构兜底 + Windows 否决）----
    # 判定策略（既精准又稳定）：
    #   · Windows 否决词（此电脑/回收站/控制面板/网络/快速访问）命中
    #       → 左栏是系统资源管理器/桌面，绝非企微 → 直接忽略（防止误删！）
    #   · 企微专属词（企业协同/应用中心/工作台/通讯录/微盘）命中
    #       → 高置信 DELETE（这些词 Windows/好生意 100% 没有）
    #   · n_strong ≥ 2（邮件/日程/会议/待办 多词共存）
    #       → 高置信 DELETE（好生意等绝不共存，精度最高）
    #   · n_strong == 1 → 边界帧：左栏结构信号（图标+文字带）也命中 → DELETE；
    #                     否则降级为 REVIEW（人工兜底，绝不自动误删）
    #   · 其余 → 忽略（不靠颜色/结构，避免好生意误删）
    # 容错：关键词匹配带编辑距离≤1 的形近/音近错字宽容（邮们/文挡/会仪…）。
    # ═══ 断点续跑（2026-08-20 加）：OCR 每帧 ~2.5s，32 分钟视频 965 帧 ≈ 40min；
    # 中途崩溃/Ctrl+C 不会白跑。进度 pickle 到 tmp/inspect_progress.pkl，第二次
    # 启动时自动跳过已 OCR 的帧（按 t 精确匹配），用户无需任何操作。
    progress_path = os.path.join(tmp, "inspect_progress.pkl")
    done_ts: set = set()
    suspects: List[Dict[str, Any]] = []  # {t, path, conf, reason}
    if os.path.exists(progress_path):
        try:
            with open(progress_path, "rb") as f:
                blob = pickle.load(f)
            if (blob.get("video") == video and
                    abs(blob.get("duration", -1) - dur) < 0.5 and
                    blob.get("step") == c["inspect_low_freq_step"]):
                done_ts = set(blob.get("done_ts", []))
                suspects = blob.get("suspects", [])
                print(f"  [断点续跑] 已完成 {len(done_ts)}/{len(low_samples)} 帧 OCR，"
                      f"从第 {len(done_ts)} 帧继续", flush=True)
        except Exception as e:
            print(f"  [断点续跑] 进度文件损坏（{e}），从头开始", flush=True)

    total = len(low_samples)
    ocr_iter = ((t, p) for t, p in low_samples if t not in done_ts)
    _t_ocr_start = time.time()
    for i, (t, path) in enumerate(ocr_iter, start=len(done_ts) + 1):
        info = _ocr_analyze_wechat(path)
        if info["windows_veto"]:
            pass  # Windows 资源管理器/桌面：不是企微，忽略
        elif len(info["app_terms"]) >= 1 or info["n_strong"] >= 2:
            suspects.append({"t": t, "path": path, "conf": 0.95,
                             "reason": "企业微信左栏(OCR:" + info["texts"] + ")"})
        elif info["n_strong"] == 1:
            # 单强词边界：左栏结构信号也命中 → 转 REVIEW（人工兜底，不自动误删，
            # 因好生意等 SaaS 左栏也有图标+文字结构，单常见词不足以定案）
            feat = _features(path)
            if (feat["left_nav_bar"] >= c.get("thr_left_nav_score", 0.30)
                    and feat["left_nav_bands"] >= 3):
                suspects.append({"t": t, "path": path, "conf": 0.60,
                                 "reason": "疑似企微单关键词(OCR+结构:" + info["texts"] + ")"})
            else:
                suspects.append({"t": t, "path": path, "conf": 0.50,
                                 "reason": "疑似企微单关键词(OCR:" + info["texts"] + ")"})
        done_ts.add(t)
        # 进度心跳：ETA 用「实测平均速度」外推（2026-08-20 改，不再用乐观 2.5s 常数）
        if i % 10 == 0 or i == total:
            pct = i * 100.0 / total
            avg = (time.time() - _t_ocr_start) / i  # 实测每帧耗时
            eta_min = (total - i) * avg / 60.0
            print(f"  [OCR 进度] {i}/{total} ({pct:.0f}%) | 实测 {avg:.1f}s/帧 | "
                  f"企微段 {len(suspects)} | ETA ~{eta_min:.0f} 分钟",
                  flush=True)
        # 落盘：每 10 帧 pickle 一次（崩溃/Ctrl+C 可恢复）
        if i % 10 == 0:
            try:
                with open(progress_path, "wb") as f:
                    pickle.dump({"video": video, "duration": dur,
                                 "step": c["inspect_low_freq_step"],
                                 "done_ts": list(done_ts),
                                 "suspects": suspects}, f)
            except Exception:
                pass
    suspects.sort(key=lambda s: s["t"])
    result["suspect_count"] = len(suspects)
    # 完成后清理进度文件
    try:
        os.remove(progress_path)
    except OSError:
        pass

    if not suspects:
        _write_report(tmp, result)
        return result

    # ---- 阶段 3：合并相邻企微帧为连续段 ----
    gap = c.get("seg_gap_sec", 2.5)
    pad = c.get("seg_pad_sec", 1.0)
    groups: List[List[Dict[str, Any]]] = [[suspects[0]]]
    for s in suspects[1:]:
        if s["t"] - groups[-1][-1]["t"] <= gap:
            groups[-1].append(s)
        else:
            groups.append([s])
    thumbs_dir = os.path.join(tmp, "review_thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)

    all_candidates: List[Dict[str, Any]] = []
    for g in groups:
        gts = [s["t"] for s in g]
        # ═══ 边界精修：覆盖「点开/点走企业微信」的过渡动作 ═══
        rs = _refine_edge(video, gts[0], -1, c, dur)
        re = _refine_edge(video, gts[-1], +1, c, dur)
        start = max(0.0, rs - pad)
        end = min(dur, re + pad)
        if end - start < c["min_segment_sec"]:
            continue
        # 段置信度 = 组内最高帧置信度；含 ≥2 强词帧 → 直接删除，否则转人工
        gconf = max(s["conf"] for s in g)
        has_multi = any(s["conf"] >= 0.90 for s in g)
        seg = {
            "start": round(start, 3),
            "end": round(end, 3),
            "type": "high_risk_screen",
            "reason": ("企业微信左栏(OCR多词确认)" if has_multi
                       else "企业微信左栏(OCR单词+结构确认)"),
            "confidence": round(gconf, 3),
            "suggestion": "delete" if gconf >= 0.70 else "review",
            "source": "screen_inspect",
            "thumbnail": "",
        }
        # 抽取代表帧缩略图（取区间中点），供人工看图核对
        mid_t = (start + end) / 2.0
        thumb_name = f"seg_{len(all_candidates) + 1:04d}.png"
        thumb_path = os.path.join(thumbs_dir, thumb_name)
        if _extract_frame(video, mid_t, thumb_path, width=c["inspect_thumb_width"]):
            seg["thumbnail"] = thumb_path
        all_candidates.append(seg)

    # ---- 阶段 4：分级 & 合并去重 & 写入 plan.json ----
    c_del = c.get("conf_delete", DEFAULTS["conf_delete"])
    c_rev = c.get("conf_review", DEFAULTS["conf_review"])

    all_delete = [s for s in all_candidates if s.get("confidence", 0) >= c_del]
    all_review = [s for s in all_candidates if c_rev <= s.get("confidence", 0) < c_del]

    result["delete_segments"] = all_delete
    result["review_items"] = all_review

    _merge_to_plan(plan_path, all_delete, all_review)
    _write_report(tmp, result)
    _write_gallery(workdir or tmp, plan_path)
    return result


def _cluster(suspects: List[Dict], gap: float) -> List[Tuple[float, List]]:
    """将疑似帧按时间聚类，返回 [(中心时间, 成员列表)]。"""
    if not suspects:
        return []
    groups: List[List[Dict]] = [[suspects[0]]]
    for s in suspects[1:]:
        if s["t"] - groups[-1][-1]["t"] <= gap:
            groups[-1].append(s)
        else:
            groups.append([s])
    return [(sum(m["t"] for m in g) / len(g), g) for g in groups]


def _find_span(frames: List[Dict], lo: float, hi: float, min_len: float) -> Optional[Tuple[float, float]]:
    """从已标记为风险的帧中找连续起止区间。"""
    if not frames:
        return None
    ts = sorted(set(s["t"] for s in frames))
    # 找第一个和最后一个风险帧
    start, end = ts[0], ts[-1]
    if end - start < min_len:
        return None
    return (start, end)


def _overlap(a: Dict[str, Any], b: Dict[str, Any], eps: float = 0.5) -> bool:
    """两区间是否时间重叠（含 eps 容差）。"""
    return a["start"] <= b["end"] + eps and b["start"] <= a["end"] + eps


def _covers(big: Dict[str, Any], small: Dict[str, Any], eps: float = 0.5) -> bool:
    """big 是否覆盖 small 的全部范围。"""
    return (big["start"] - eps <= small["start"]
            and small["end"] <= big["end"] + eps)


def _merge_to_plan(plan_path: str, deletes: List[Dict], reviews: List[Dict]):
    """将巡检结果追加写入 plan.json 的 delete_segments 和 review_items。

    v3: 高置信候选直接进 delete_segments（自动删除），
        中置信进 review_items（人工兜底）。
    """
    if not os.path.exists(plan_path):
        return
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    existing_deletes = plan.get("delete_segments", [])
    existing_reviews = plan.get("review_items", [])

    new_ds = _merge_spans(existing_deletes + deletes)
    new_rw = _merge_spans(existing_reviews + reviews)

    # 确保 review 不被 delete 完全覆盖（保留需人工看的）
    kept_reviews = []
    for rw in new_rw:
        covered = any(d["start"] - 0.5 <= rw["end"] and rw["start"] <= d["end"] + 0.5
                      for d in new_ds)
        if not covered:
            kept_reviews.append(rw)

    plan["delete_segments"] = new_ds
    plan["review_items"] = kept_reviews
    plan["screen_inspected"] = True

    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def _merge_spans(items: List[Dict]) -> List[Dict]:
    """按 start 合并重叠区间。"""
    if not items:
        return []
    s = sorted(items, key=lambda x: x["start"])
    out = [dict(s[0])]
    for it in s[1:]:
        if it["start"] <= out[-1]["end"] + 0.5:
            out[-1]["end"] = max(out[-1]["end"], it["end"])
            out[-1]["confidence"] = max(out[-1].get("confidence", 0),
                                          it.get("confidence", 0))
        else:
            out.append(dict(it))
    return out


def _write_report(workdir: str, result: Dict):
    """写出巡检报告 JSON。"""
    report_path = os.path.join(workdir, "screen_inspect_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _write_gallery(workdir: str, plan_path: str):
    """生成带缩略图的人工审核画廊 HTML，便于人工看图核对高风险候选。

    列出 plan.json 中全部 review_items（含 ASR 敏感片段与画面巡检候选），
    画面巡检候选附代表帧缩略图。该页面只供查看，删除决定通过
    `main.py confirm` 或对话中告知 AI 后执行。
    """
    from .timeline import fmt_hms_short

    if not os.path.exists(plan_path):
        return
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    items = plan.get("review_items", []) or []

    rows = []
    for i, r in enumerate(items, 1):
        ts_s = fmt_hms_short(r.get("start", 0.0))
        ts_e = fmt_hms_short(r.get("end", 0.0))
        dur = float(r.get("end", 0.0)) - float(r.get("start", 0.0))
        conf = float(r.get("confidence", 0.0))
        src = r.get("source", "")
        src_lab = "画面巡检" if src == "screen_inspect" else (
            "ASR敏感" if src else "其他")
        reason = (r.get("reason", "") or "").replace("<", "&lt;").replace(">", "&gt;")
        thumb = r.get("thumbnail", "") or ""
        if thumb:
            img = (f'<img class="thumb" src="file:///{thumb.replace(chr(92), "/")}" '
                   f'alt="帧" onerror="this.style.display=\'none\'">')
        else:
            img = '<div class="nothumb">无缩略图</div>'
        action_hint = ("建议删除" if r.get("suggestion", "delete") == "delete"
                       else "建议保留")
        rows.append(f"""
    <div class="card">
      <div class="idx">#{i}</div>
      <div class="imgcol">{img}</div>
      <div class="metacol">
        <div class="time">{ts_s} - {ts_e} <span class="dur">({dur:.1f}s)</span></div>
        <div class="tags">
          <span class="tag">{src_lab}</span>
          <span class="tag conf">置信度 {conf:.2f}</span>
          <span class="tag sug">{action_hint}</span>
        </div>
        <div class="reason">{reason}</div>
      </div>
    </div>""")

    cards = "\n".join(rows) or '<p class="empty">（没有待人工确认的片段）</p>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>高风险画面审核画廊</title>
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif;
         background:#f5f6f8; color:#222; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#666; font-size:13px; margin-bottom:16px; }}
  .warn {{ background:#fff4e5; border:1px solid #ffd591; color:#8a5a00;
          padding:10px 14px; border-radius:8px; font-size:13px; margin-bottom:18px; }}
  .card {{ display:flex; gap:14px; background:#fff; border:1px solid #e6e8eb;
          border-radius:10px; padding:12px; margin-bottom:12px; }}
  .idx {{ font-weight:700; color:#1677ff; min-width:30px; font-size:15px; }}
  .imgcol {{ flex:0 0 240px; }}
  .thumb {{ width:240px; border-radius:6px; border:1px solid #eee; display:block; }}
  .nothumb {{ width:240px; height:135px; background:#fafafa; border:1px dashed #ccc;
             border-radius:6px; display:flex; align-items:center;
             justify-content:center; color:#999; font-size:12px; }}
  .metacol {{ flex:1; }}
  .time {{ font-weight:600; font-size:15px; }}
  .dur {{ color:#888; font-weight:400; font-size:12px; }}
  .tags {{ margin:6px 0; }}
  .tag {{ display:inline-block; font-size:12px; padding:2px 8px; border-radius:10px;
          margin-right:6px; background:#eef2f7; color:#555; }}
  .tag.conf {{ background:#e6f4ff; color:#1677ff; }}
  .tag.sug {{ background:#fff1f0; color:#cf1322; }}
  .reason {{ font-size:13px; color:#555; line-height:1.5; word-break:break-all; }}
  .empty {{ color:#999; }}
</style>
</head>
<body>
  <h1>高风险画面 · 人工审核画廊</h1>
  <div class="sub">源视频：{os.path.basename(plan.get('source', ''))} ｜ 共 {len(items)} 项待人工确认</div>
  <div class="warn">⚠️ 隐私红线：以上片段<strong>尚未被删除</strong>。请逐张查看缩略图，
    确认哪些确实含客户电话/私聊等企业微信隐私内容后，再决定删除。
    删除方式：运行 <code>python main.py confirm --plan plan.json --action delete --items 序号</code>
    （或对话中告知 AI 要删哪些时间段），随后 <code>render</code>。</div>
  {cards}
</body>
</html>"""
    out_path = os.path.join(workdir, "review_gallery.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ---------------------------------------------------------------------------
# CLI 入口（供 main.py inspect 子命令调用）
# ---------------------------------------------------------------------------
def main_cli(args):
    """inspect 子命令入口。"""
    video = os.path.abspath(args.input)
    plan_path = args.plan or os.path.join(
        args.workdir or "./_work", "plan.json"
    )
    workdir = os.path.abspath(args.workdir) if args.workdir else os.path.dirname(plan_path)

    if not os.path.exists(video):
        print(f"[错误] 视频文件不存在: {video}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(plan_path):
        print(f"[错误] plan.json 不存在: {plan_path}（请先运行 analyze）", file=sys.stderr)
        sys.exit(1)

    print(f"[巡检] 开始高风险画面巡检...")
    print(f"       视频: {video}")
    print(f"       plan: {plan_path}")
    print(f"       低频步长: {DEFAULTS['inspect_low_freq_step']}s | "
          f"加密窗口: ±{DEFAULTS['inspect_dense_window']}s")

    result = run(video, plan_path, workdir=workdir)

    nd = len(result["delete_segments"])
    nr = len(result["review_items"])
    gallery = os.path.join(workdir, "review_gallery.html")
    print(f"[完成] 巡检结束（v3：左侧导航栏主特征 + 自动删除）。")
    print(f"       低频抽样 {result['low_freq_frames']} 帧 | "
          f"疑似 {result['suspect_count']} 处 | "
          f"加密抽样 {result['dense_frames']} 帧")
    print(f"       → 自动删除: {nd} 个 | 人工兜底: {nr} 个")
    if nd:
        for d in result["delete_segments"]:
            print(f"         DELETE [{d['start']:.1f}-{d['end']:.1f}s] "
                  f"conf={d['confidence']:.2f} {d['reason']}")
    if nr:
        for i, r in enumerate(result["review_items"], 1):
            print(f"         REVIEW #{i} [{r['start']:.1f}-{r['end']:.1f}s] "
                  f"conf={r['confidence']:.2f} {r['reason']}")
        print(f"       审核画廊（带缩略图）: {gallery}")
        print(f"       确认删除: python main.py confirm --plan {plan_path} "
              f"--action delete --items 序号")
    elif not nd:
        print("       未检出高风险画面。")


if __name__ == "__main__":
    import sys
    class FakeArgs:
        input = sys.argv[1] if len(sys.argv) > 1 else ""
        plan = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--plan" else None
        workdir = None
    main_cli(FakeArgs())
