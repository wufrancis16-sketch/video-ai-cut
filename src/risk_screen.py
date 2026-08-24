"""企业微信检测（稳定性优先版 2026-08-21）。

设计目标（用户调整优先级：P0 成片质量与稳定性 > 速度）：
- 自动删除企业微信页面，**绝不误删好生意等 ERP 产品演示页**。
- 不为追求速度牺牲准确率（不要为了减少耗时牺牲准确率）。

三级稳定策略
============
1. **全片低帧率 OCR 扫描（召回优先，不依赖 ASR）**：始终对全片按
   risk_screen_sample_step 步长抽帧评分，不靠"ASR 是否提到企微"做开关
   （旧版曾因"展示了企微界面却没口头提"而整体跳过 → 漏删）。用户硬要求
   "不能漏删"，宁可多扫。
2. **OCR 只做"确认"，不直接决定删除**：在每帧上用 OCR 抽取左栏导航词 +
   聊天区/联系人特征，给出置信度评分。高置信 → 删除；中置信 → 进审核
   清单（不删）；低置信 → 保留。
3. **否决机制兜底（绝不误删）**：左栏命中 Windows 桌面词 / 腾讯会议词
   → 直接否决（不给分）。腾讯会议否决**仅当 n_strong==0 时生效**（企微聊天
   正文可能提到"腾讯会议"，但那种帧仍有企微强导航 → 照删保召回）；且否决词
   只放**腾讯会议产品名**（快速会议/预定会议/加入会议 等是**企业微信自己的
   会议按钮**，放进来会误否决真企微帧 → 漏删，实测踩过坑）。

置信度评分（用户指定维度）
==========================
左栏导航词（来自用户清单）——**全部精确匹配，绝不用编辑距离模糊匹配**：
  强特征（企微专属，好生意/畅捷通左栏绝不含）→ 命中 +3；≥2 共存再 +2
    · 邮件 / 文档 / 日程 / 会议（"会议"仅指独立导航 tab，会议号/快速会议等
      复合词不算，见 _has_meeting_standalone）
  弱特征（企微与好生意共有，仅作辅助）→ 每个 +1（最多 +2）
    · 消息 / 通讯录 / 待办
聊天区特征：绿气泡 UI → +2
联系人/群聊特征：整帧含 联系人/群聊/群成员/通讯录/微信群 → +2
应用专有词：整帧含 企业微信/企微/微信群/交付群/客户群/群聊/微信聊天 → +2

⚠️ **为什么必须精确匹配（2026-08-21 实测教训）**：好生意等 ERP 产品页文字密集，
模糊匹配（编辑距离≤1）会把 日期→日程、协议→会议、销售→消息、客户账本→客户群
等形近词误判成企微强特征/专有词，绕过产品页排除 → **产品页被整段误删**
（实测 t=1500/1800 误判 score 8.5/8.0）。真企微左栏 6~7 个导航词 OCR 基本
都能读准（实测 strong=4），多词冗余已足够召回，不需要模糊匹配冒险。

分级：score >= 5 自动删除；3 <= score < 5 进审核清单；score < 3 保留。

产品页排除（避免误删好生意，关键稳定性保障）
==========================================
产品页词（好生意/ERP 正常产品展示）：商品/库存/规格/单位/价格/销售价/
采购价/成本价/单价/零售价/批发价/会员价。
规则：仅当「无强企微证据」（左栏没有精确的 邮件/文档/日程/会议）时，产品页词
才把 score 压到 review 阈值以下 → 绝不自动删除。理由：好生意左栏永不出现
强企微词，所以真企微界面（带强词）即便聊天里提到"订单/商品"也照删；而
纯好生意产品页（无强词）无论弱信号多高都被压住，从根上杜绝误删。

稳定性保障
==========
- ASR 完成后 analyze.py 已调用 holder.unload() 释放 Whisper，OCR 阶段只有
  RapidOCR + 帧缓冲，避免 Whisper+RapidOCR 共存 OOM。
- 边界扩展**不再**做每方向数十次密集 OCR 精修（旧 _refine 累积数百次调用 →
  RapidOCR 内存不释放 → OOM SIGKILL），改为采样窗口极端帧 ±(pad+REFINE_BUF)。
- OCR 引擎按帧数周期重置（si._OCR=None + gc），防止长视频累积内存泄漏。
"""
from __future__ import annotations

import gc
import os
import shutil
import tempfile
from typing import List, Dict, Any, Optional, Tuple

from . import timeline as T
from .config import Config
from .utils import run, get_duration
from . import screen_inspect as si   # 复用精确左栏 OCR 工具（_ocr_full_boxes / _extract_frame）


# ===========================================================================
# 词表与评分阈值（集中管理，便于调参）
# ===========================================================================
# ---- ASR 召回关键词（仅企微专有 / 强提及）----
# 严禁：客户 / 联系人 / 聊天 / 价格 等 ERP 演示高频通用词（它们会触发海量误候选）。
KEYWORDS = ["企业微信", "企微", "微信聊天", "微信群", "客户群", "交付群",
            "群聊", "群里面", "发群里", "微信里"]

# ---- 左栏导航词 ----
# 强特征：企微专属，好生意/畅捷通/畅捷通好生意左栏实测绝不含这些词
#   （其导航为 客户中心/销售管理/采购管理/库存管理/报表中心…），故为删除级强证据。
NAV_STRONG_OCR = ["邮件", "文档", "日程", "会议"]
# 弱特征：企微与好生意共有（好生意左栏也有 消息/待办），仅作辅助，不足以单独定案。
NAV_COMMON_OCR = ["消息", "通讯录", "待办"]

# ---- 整帧应用专有词（命中即强确认，OCR 整帧文字出现"企业微信"等）----
APP_TERMS_OCR = ["企业微信", "企微", "微信群", "交付群", "客户群", "群聊", "微信聊天"]
# ---- 联系人/群聊特征（整帧文字出现即视为聊天区/通讯录）----
CONTACT_TERMS_OCR = ["联系人", "群聊", "群成员", "通讯录", "微信群"]

# ---- 产品页排除词（好生意/ERP 正常产品展示，绝不应被消音/删除）----
PRODUCT_KEYS = ["商品", "库存", "规格", "单位", "价格", "销售价", "采购价",
                "成本价", "单价", "零售价", "批发价", "会员价"]

# ---- 腾讯会议否决词（等候室/会议界面专属，与企业微信"会议"导航 tab 完全不同）----
# 用户视频大量以「腾讯会议」屏幕共享方式录制，开场即等候室（含 会议号/创建者/
# 腾讯会议 标题等）。命中即判定为腾讯会议界面，绝非企业微信，直接否决（不给分）。
#
# 安全约束（2026-08-21 实测修正，防止误否决真企微帧 → 漏删）：
#  · MEETING_VETO_KEYS 只放**腾讯会议产品专属词**（"腾讯会议"产品名）。
#    ⚠️ 不能放 快速会议/预定会议/加入会议/视频会议/会议录制/等候室/离开会议/
#       结束会议 —— 这些是**企业微信自己的会议功能按钮**，真企微界面里就有
#       （实测视频① 真企微帧 FULL_OCR 含"快速会议"），放进来会把真企微帧
#       误判成腾讯会议 → 整段漏删！
#  · 否决必须**附带 n_strong==0 条件**：企微聊天正文里可能提到"腾讯会议"
#    （如分享会议链接），但那种帧仍有企微左栏强导航词（邮件/文档/日程/会议）；
#    有强词 → 不否决（照删，保召回）；无强词且含腾讯会议专属词 → 才是会议界面。
#  · "会议号" 仅 3 字且模糊匹配会撞形近「会议日」(企微 会议+日程 导航相邻)，
#    故 MEETING_VETO_EXACT 仅做精确匹配；且有 n_strong==0 条件兜底。
MEETING_VETO_KEYS = ["腾讯会议"]
MEETING_VETO_EXACT = ["会议号"]

# "会议"作为企微导航 tab 必须是独立词，不能夹在会议复合词里
# （会议号/快速会议/预定会议/加入会议/会议室/视频会议 等是腾讯会议专属）。
_MEETING_COMPOUNDS = ["会议号", "快速会议", "预定会议", "加入会议", "会议室",
                      "视频会议", "会议录制", "腾讯会议", "网络会议", "语音会议",
                      "会议设置", "会议管理"]

# ---- 置信度评分阈值 ----
SCORE_DELETE = 5.0     # >= 此分自动删除
SCORE_REVIEW = 3.0     # 3~5 进 review（待人工确认）；<3 保留
# 边界外扩缓冲（覆盖"点开/点走企微"的过渡动作，无额外 OCR）
REFINE_BUF = 1.2


# ===========================================================================
# 候选窗口（ASR 关键词）
# ===========================================================================
def candidate_windows(segments: List[Dict[str, Any]], dur: float,
                      cfg: Config) -> List[Tuple[float, float]]:
    """从 ASR 文本找企微关键词，生成候选时间窗口（合并重叠）。"""
    pad = cfg.risk_screen_keyword_pad
    wins: List[Tuple[float, float]] = []
    for seg in segments:
        text = seg.get("text", "") or ""
        if not text:
            continue
        if any(k in text for k in KEYWORDS):
            s = float(seg.get("start", 0.0)) - pad
            e = float(seg.get("end", 0.0)) + pad
            wins.append((max(0.0, s), min(dur, e)))
    return T.merge_ranges(wins)


# ===========================================================================
# 单帧置信度评分（左栏 OCR + 整帧 OCR + UI 信号）
# ===========================================================================
def _ui_signals(img_path: str) -> Tuple[bool, bool]:
    """轻量 UI 信号：聊天气泡(绿) + 企微蓝标题栏。

    返回 (bubble, blue)：
      bubble → 画面含微信绿气泡（#95EC69 附近）→ 聊天区特征 +2
      blue   → 顶部深色标题栏（企微蓝 UI 辅助）→ +0.5（仅作微弱辅助）
    """
    try:
        from PIL import Image
        img = Image.open(img_path).convert("RGB").resize((360, 640))
    except Exception:
        return False, False
    px = list(img.getdata())
    n = len(px) or 1
    # 微信聊天绿气泡 #95EC69 ≈ (149,236,105)
    bubble = sum(1 for r, g, b in px
                 if g > 180 and b > 70 and r < g - 40 and g - r > 60) / n
    w, h = img.size
    top = list(img.crop((0, 0, w, max(1, h // 16))).getdata())
    top_dark = sum(1 for r, g, b in top if (r + g + b) < 360) / len(top)
    return (bubble > 0.01), (top_dark > 0.3)


def _has_meeting_standalone(joined: str) -> bool:
    """"会议" 仅当是企微独立导航 tab 才算强特征，绝不能夹在腾讯会议复合词里。

    腾讯会议等候室/会议界面含 会议号/快速会议/预定会议/加入会议/会议室/视频会议
    等，其"会议"是复合词一部分，绝非企微左栏"会议"导航项。命中任何复合词即视为
    腾讯会议语境，不把"会议"算作企微强证据（配合 MEETING_VETO_KEYS 双重保险）。
    """
    if any(c in joined for c in _MEETING_COMPOUNDS):
        return False
    return "会议" in joined


def _score_frame(img_path: str, cfg: Config) -> Dict[str, Any]:
    """对单帧做企微置信度评分（0~+∞，无强企微证据的产品页强制 <=2）。

    召回优先（P0 稳定性 > 速度，且"不能漏删"硬要求）：
    - 左栏用**高分辨率 OCR**（si._ocr_wechat_texts：左 14% 裁切 + 上采样 +
      对比拉伸）——这是经实测能稳定读出 邮件/文档/日程/会议 小字号导航词
      的唯一路径；旧版整帧降采样到 640px 会读不到左栏小字而漏检。
    - 仅当左栏出现任何企微线索（强/弱导航词、app 词）或检测到聊天气泡时，
      才额外做整帧 OCR（si._ocr_full_texts）用于 联系人/产品页排除/确认。
      好生意等干净帧（左栏无任何企微词、无气泡）→ 跳过整帧 OCR → 省时且
      绝不会因漏读而误删。
    """
    # ① 左栏高分辨率 OCR（可靠读取 邮件/文档/日程/会议）
    try:
        left_texts = si._ocr_wechat_texts(img_path)
    except Exception:
        left_texts = []
    left = "".join(left_texts)

    # Windows 桌面/资源管理器否决（左栏出现此电脑/回收站等 → 绝非企微）
    windows_veto = any(si._fuzzy_has(left, k) for k in si.WINDOWS_VETO_KEYS)
    if windows_veto:
        return {"score": -100, "product": False,
                "reason": "Windows桌面/资源管理器",
                "nav": 0, "strong": 0, "app": False, "bubble": False,
                "blue": False, "contacts": False}

    # 强企微导航词：邮件/文档/日程/会议——**必须精确命中**（不能用编辑距离模糊匹配）。
    # 教训（2026-08-21 实测）：好生意等 ERP 产品页文字密集，模糊匹配会把
    #   "日期"→"日程"、"协议"→"会议"、"销售"→"消息" 等形近词误判成企微强特征，
    #   绕过产品页排除规则 → 产品页被整段误删（t=1500/1800 实测 score 8.5/8.0）。
    #   真企微左栏 6~7 个导航词 OCR 基本都能读准（实测视频① strong=4），
    #   多词冗余已足够召回，不需要用模糊匹配冒险。
    # "会议" 必须是独立导航 tab，不能夹在会议复合词里（见 _has_meeting_standalone）。
    n_strong = 0
    for k in NAV_STRONG_OCR:
        if k == "会议":
            n_strong += 1 if _has_meeting_standalone(left) else 0
        elif k in left:
            n_strong += 1
    n_common = sum(1 for k in NAV_COMMON_OCR if k in left)
    app_hit = any(k in left for k in APP_TERMS_OCR)

    # 腾讯会议否决（等候室/会议界面：会议号/腾讯会议… 绝非企微）。
    # 关键条件：**仅当 n_strong==0 才否决**——企微聊天正文里也可能提到"腾讯会议"
    # （分享会议链接），但那种帧仍有企微左栏强导航词；有强词 → 不否决（照删保召回），
    # 无强词且含腾讯会议专属词 → 才是纯会议界面（开场等候室/中段会议页）。
    # 左栏裁切可能漏掉画面中部的"腾讯会议"大标题，故整帧 OCR 后再复核一次。
    meeting_veto = n_strong == 0 and (
        any(k in left for k in MEETING_VETO_EXACT)
        or any(si._fuzzy_has(left, k) for k in MEETING_VETO_KEYS))
    if meeting_veto:
        return {"score": -100, "product": False,
                "reason": "腾讯会议界面(非企微)",
                "nav": 0, "strong": 0, "app": False, "bubble": False,
                "blue": False, "contacts": False}

    # 轻量 UI 信号（绿气泡/蓝标题栏），无 OCR 成本
    bubble, blue = _ui_signals(img_path)

    # ② 仅"可疑帧"做整帧 OCR（联系人/产品页排除/确认）；干净帧跳过
    full = ""
    contacts = False
    product = False
    suspicious = (n_strong >= 1) or (n_common >= 1) or app_hit or bubble
    if suspicious:
        try:
            full_texts = si._ocr_full_texts(img_path)
        except Exception:
            full_texts = []
        full = "".join(full_texts)
        # 整帧再复核腾讯会议（左栏裁切可能漏掉画面中部"腾讯会议"大标题/会议号）
        if n_strong == 0 and (
                any(k in full for k in MEETING_VETO_EXACT)
                or any(si._fuzzy_has(full, k) for k in MEETING_VETO_KEYS)):
            return {"score": -100, "product": False,
                    "reason": "腾讯会议界面(非企微)",
                    "nav": 0, "strong": 0, "app": False, "bubble": False,
                    "blue": False, "contacts": False}
        contacts = any(k in full for k in CONTACT_TERMS_OCR)
        product = any(k in full for k in PRODUCT_KEYS)
        if not app_hit:
            app_hit = any(k in full for k in APP_TERMS_OCR)
    # 非可疑帧 score 必然 < 3（无导航词/无 app/无气泡），产品页排除无意义，跳过。

    score = 0.0
    if n_strong >= 1:
        score += 3.0
    if n_strong >= 2:
        score += 2.0          # 两个强企微专属词共存（如 会议+日程）→ 直接达删除级
    score += min(n_common, 2) * 1.0
    if app_hit:
        score += 2.0
    if contacts:
        score += 2.0
    if bubble:
        score += 2.0
    if blue:
        score += 0.5

    reason = []
    if n_strong >= 1:
        reason.append(f"强企微导航×{n_strong}(邮件/文档/日程/会议)")
    elif n_common >= 1:
        reason.append(f"弱企微导航×{n_common}(消息/通讯录/待办)")
    if app_hit:
        reason.append("企微专有词")
    if contacts:
        reason.append("联系人/群聊")
    if bubble:
        reason.append("聊天气泡")
    if product:
        reason.append("产品页词(排除)")

    # 产品页排除：仅当"无强企微证据"时压低，绝不自动删除好生意产品页。
    # 真企微界面（带强词）即使聊天里提到商品/订单，强词已证明是企微 → 照删。
    if product and n_strong == 0:
        score = min(score, SCORE_REVIEW - 1.0)

    return {"score": score, "product": product,
            "reason": ";".join(reason) or "无企微特征",
            "nav": n_common, "strong": n_strong, "app": app_hit,
            "bubble": bubble, "blue": blue, "contacts": contacts}


# ===========================================================================
# 抽帧 + 采样（候选窗口内 / 静音兜底全片）
# ===========================================================================
def _extract_frame(video: str, t: float, out_path: str) -> bool:
    return si._extract_frame(video, t, out_path, width=1280)


def _sample_window(video: str, t0: float, t1: float, step: float,
                   cfg: Config, tmp: str) -> List[Dict[str, Any]]:
    """在 [t0, t1] 内按步长抽帧并评分，返回按时间排序的样本列表。

    周期性重置 OCR 引擎（每 ocr_reset_every 帧）防止长视频内存泄漏 OOM；
    仅释放 Whisper 后仍驻留的 RapidOCR 累积内存。
    """
    out: List[Dict[str, Any]] = []
    reset_every = int(getattr(cfg, "ocr_reset_every", 40) or 40)
    t = t0
    cnt = 0
    while t <= t1 + 1e-6:
        img = os.path.join(tmp, f"r{int(round(t * 1000))}.png")
        if _extract_frame(video, t, img):
            s = _score_frame(img, cfg)
            s["t"] = round(t, 3)
            out.append(s)
            cnt += 1
            if reset_every and cnt % reset_every == 0:
                # 释放并惰性重载 RapidOCR，遏制单次检测内累积内存
                try:
                    si._OCR = None
                except Exception:  # noqa
                    pass
                gc.collect()
        t += step
    out.sort(key=lambda x: x["t"])
    return out


# ===========================================================================
# 边界扩展：从候选窗口的样本里找出连续高/中置信段并扩展边界（无额外 OCR）
# ===========================================================================
def _group(frames: List[Dict[str, Any]], step: float) -> List[List[Dict[str, Any]]]:
    """将连续帧（间隔 <= step+0.6）聚成组。"""
    if not frames:
        return []
    frames = sorted(frames, key=lambda x: x["t"])
    groups: List[List[Dict[str, Any]]] = [[frames[0]]]
    for s in frames[1:]:
        if s["t"] - groups[-1][-1]["t"] <= step + 0.6:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


def _expand_runs(samples: List[Dict[str, Any]], video: str, dur: float,
                 cfg: Config) -> Tuple[List[Dict], List[Dict]]:
    """从窗口样本里分出连续 delete 段与 review 段并扩展边界。

    边界 = 命中极端帧 ±(pad + REFINE_BUF)，**不再**调用旧 _refine 做密集 OCR
    精修（旧 _refine 累积数百次 OCR → OOM SIGKILL）。候选窗口已 ±max_expand
    覆盖过渡区，采样步长 2s + pad(1s) + REFINE_BUF(1.2s) 足以夹住点击动作，
    边界误差 ±~4s 对连续展示几十秒的企微界面可接受；且优先级 误删>漏删。
    """
    step = cfg.risk_screen_sample_step
    pad = cfg.sensitive_screen_pad
    min_screen = cfg.risk_screen_min_screen
    buf = REFINE_BUF

    # 注：产品页排除已在 _score_frame 内完成（仅当"无强企微证据"时把 score
    # 压到 <=2）。达到 SCORE_DELETE 的帧必然带强企微证据（否则已被压低），
    # 即使聊天里提到商品/订单也属于真实企微界面 → 照删。故此处不再二次排除 product。
    del_frames = [s for s in samples if s["score"] >= SCORE_DELETE]
    rev_frames = [s for s in samples
                  if SCORE_REVIEW <= s["score"] < SCORE_DELETE]

    deletes: List[Dict] = []
    reviews: List[Dict] = []

    def _build(group_frames: List[Dict], action: str) -> Optional[Dict]:
        gts = [s["t"] for s in group_frames]
        start = max(0.0, gts[0] - pad - buf)
        end = min(dur, gts[-1] + pad + buf)
        if end - start < min_screen:
            # 过短：删除级降级为 review；review 级直接丢弃（防误删闪帧）
            if action == "delete":
                action = "review"
            else:
                return None
        reason = "; ".join(sorted({s.get("reason", "") for s in group_frames
                                   if s.get("reason")}))[:200] or "企微界面（客户隐私）"
        return {"start": float(start), "end": float(end),
                "reason": reason, "type": "高风险画面", "action": action}

    for g in _group(del_frames, step):
        d = _build(g, "delete")
        if d:
            (deletes if d["action"] == "delete" else reviews).append(d)
    for g in _group(rev_frames, step):
        r = _build(g, "review")
        if r:
            reviews.append(r)
    return deletes, reviews


# ===========================================================================
# 主入口
# ===========================================================================
def detect(video: str, segments: List[Dict[str, Any]], llm,
           cfg: Config, fallback: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """企业微信检测（全片低帧率 OCR 扫描 + 置信度评分 + 产品页排除）。

    召回优先（P0 稳定性 > 速度；用户硬要求"不能漏删"）：
    - **始终对全片做低帧率 OCR 扫描**，不依赖 ASR 是否提到"企业微信"。
      旧版曾用"ASR 关键词命中才扫描"作为开关，导致"展示了企微界面却没口头
      提"的视频（如本视频 t≈45s 一段）被整体跳过 → 漏删。现已改为全片扫描。
    - 评分综合 左栏强/弱导航词（高分辨率 OCR）+ app 专有词 + 联系人 + 聊天气泡；
      产品页排除规则（无强证据才压制）确保好生意等产品演示页绝不被删。
    - 边界用 _expand_runs（采样极端帧 ±(pad+REFINE_BUF)），不再做旧 _refine
      密集 OCR 精修（旧 OOM 主因）。
    - 长视频按帧数周期重置 OCR 引擎，遏制 RapidOCR 内存泄漏。
    """
    dur = 0.0
    try:
        dur = get_duration(video)
    except Exception:
        dur = 0.0
    if dur <= 0 and segments:
        dur = max(float(s.get("end", 0.0)) for s in segments)
    if dur <= 0:
        return {"delete": [], "review": []}

    # 全片低帧率扫描（召回优先，不走 ASR 关键词开关）
    step = cfg.risk_screen_sample_step
    print(f"[检测] 全片低帧率 OCR 扫描（步长 {step}s，时长 {dur:.0f}s）"
          f"{'（静默视频）' if not segments else ''}", flush=True)

    delete: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    tmp = tempfile.mkdtemp(prefix="rsk_")
    try:
        samples = _sample_window(video, 0.0, dur, step, cfg, tmp)
        if samples:
            dec, rev = _expand_runs(samples, video, dur, cfg)
            delete.extend(dec)
            review.extend(rev)
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

    # 合并重叠；删除优先于审核（已确认删除的区间不再标审核）
    delete = _merge_items(delete)
    review_kept: List[Dict[str, Any]] = []
    for r in _merge_items(review):
        if not any(d["start"] - 1e-3 <= r["end"] and r["start"] <= d["end"] + 1e-3
                   for d in delete):
            review_kept.append(r)
    return {"delete": delete, "review": review_kept}


def _merge_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return []
    items = sorted(items, key=lambda x: x["start"])
    out = [dict(items[0])]
    for it in items[1:]:
        if it["start"] <= out[-1]["end"] + 1e-6:
            out[-1]["end"] = max(out[-1]["end"], it["end"])
            out[-1]["reason"] = (out[-1]["reason"] + "；" + it["reason"]).strip("；")
        else:
            out.append(dict(it))
    return out
