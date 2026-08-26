"""第一阶段：分析（不做任何视频编码）。

    原始视频
      ↓ 提取音频（仅在 ASR 缓存未命中时执行）
      ↓ 本地 ASR（一次，结果缓存）
      ↓ 术语纠错 → 敏感数据标注 → 客户问答识别
      ↓ 议价检测（读字幕文本，LLM 或关键词）
      ↓ 长停顿 / 口头禅（读时间轴，不用 AI）
      ↓ 开场检测（少量抽帧）
      ↓ 高风险画面（ASR 关键词定位 + 少量关键帧确认，见 risk_screen.py）
      ↓
    统一剪辑时间轴 EditPlan(plan.json) + 人工审核清单

本模块**绝不**调用任何视频编码命令，也不会把整段视频交给大模型。
所有耗时结果均走 cache，改规则时可秒级重算。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import asr as asr_mod
from . import bargaining, cache, glossary, intro as intro_mod
from . import sensitive, timeline as T, subtitle_detect as subd_mod
from .audio import extract_audio
from .config import Config
from .timeline import EditPlan
from .utils import ensure_dir, probe_video

# ---- 客户提问信号（用于「客户问题优先保留」）----
QUESTION_MARKS = ("?", "？")
QUESTION_WORDS = [
    "吗", "呢", "能不能", "可不可以", "行不行", "有没有", "是不是", "怎么",
    "如何", "为什么", "多久", "几个", "哪些", "哪个", "什么时候", "能否",
    "我想问", "问一下", "请问", "咨询一下", "如果我们", "我们公司",
    "支持不支持", "可以吗", "怎么办", "怎么弄", "怎么设置",
]
# 客户自述业务场景（同样属于高价值内容，需保留）
CUSTOMER_CONTEXT = [
    "我们公司", "我们有", "我们是", "我们这边", "我们现在", "我们目前",
    "我们仓库", "我们门店", "我们工厂",
]

# ---- 纯口头禅（整句仅由这些构成时才删除）----
FILLER_TOKENS = [
    "嗯", "呃", "啊", "哦", "噢", "唉", "诶", "这个", "那个", "然后",
    "就是", "对对对", "对对", "嗯嗯", "好好好", "是是是", "呃呃",
    "怎么说呢", "怎么讲",
]
_PUNCT_RE = re.compile(r"[\s，。、！？…,\.!\?~—\-·:：;；\"'“”‘’()（）]+")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze(video: str, cfg: Config, llm=None,
            plan_path: Optional[str] = None) -> EditPlan:
    """分析视频并返回统一剪辑时间轴。"""
    video = os.path.abspath(video)
    ensure_dir(cfg.workdir)
    info = probe_video(video)
    print(f"[分析] {os.path.basename(video)}  "
          f"{info['width']}x{info['height']} @ {info['fps']:.3f}fps  "
          f"{info['duration']:.1f}s  audio={info['has_audio']}")

    plan = EditPlan(
        source=video,
        duration=float(info["duration"]),
        fps=float(info["fps"]),
        width=int(info["width"]),
        height=int(info["height"]),
        subtitle=bool(cfg.burn_subtitle),
    )

    # ---- 0.5) 已有字幕检测（仅作跳过判断，不读 ASR/字幕内容）-------------
    if cfg.skip_subtitle_if_exists:
        existing = subd_mod.detect_existing_subtitle(
            video, cfg, float(info["duration"]))
        if existing:
            plan.existing_subtitle = existing
            print(f"  [字幕判断] 原视频已含字幕（{existing['type']}），"
                  f"将跳过字幕识别与烧录")

    plan.meta = {
        "vcodec": info["vcodec"], "acodec": info["acodec"],
        "has_audio": info["has_audio"],
        "sample_rate": info["sample_rate"], "channels": info["channels"],
        "asr_model": cfg.asr_model, "llm": bool(llm and llm.available()),
        "pause_mode": cfg.pause_mode,
    }

    # ---- 1) ASR（一次 + 缓存）------------------------------------------
    segments: List[Dict[str, Any]] = []
    if info["has_audio"]:
        wav = os.path.join(cfg.workdir, "full_audio.wav")

        def _audio():
            print("  [音频] 提取整片音频")
            return extract_audio(video, wav)

        holder = asr_mod.LazyModel(cfg)
        segments = asr_mod.transcribe_video_cached(video, _audio, cfg, holder)
        print(f"  [ASR] {len(segments)} 句")
    else:
        print("  [ASR] 视频无音轨，跳过语音分析")

    # ---- 2) 文本后处理：术语纠错 + 敏感数据标注 + 角色启发 ----------
    if segments:
        segments = glossary.correct_segments(segments)
        segments = sensitive.annotate_segments(segments)
        segments = _apply_llm_sensitive(segments, llm, cfg, video)
        _mark_customer_turns(segments)

    # ---- 2.5) 释放 Whisper 模型，避免与后续 RapidOCR 检测同时驻留 OOM ----
    # ASR 已结束，后续议价/消音/停顿/开场/高风险画面检测均不再需要 Whisper。
    # 立即释放（del + gc），让 risk_screen 的 RapidOCR 独占内存，峰值大幅下降，
    # 规避「Whisper(~0.5GB)+RapidOCR」同时驻留触发 OOM SIGKILL。
    if holder.loaded:
        holder.unload()
        print("  [ASR] 已释放 Whisper 模型，腾出内存给高风险画面 OCR 检测")

    # ---- 3) 议价内容 -> delete_segments --------------------------------
    if segments and cfg.detect_bargaining:
        for span in _cached_bargaining(segments, llm, cfg, video):
            plan.add_delete(span["start"], span["end"], T.T_NEGOTIATION,
                            span.get("reason", "商务议价对话"))
        print(f"  [议价] {len(plan.delete_segments)} 段")

    # ---- 4) 敏感业务数据 -> mute_segments（不删除画面）----------------
    if segments:
        mutes = _build_mute_segments(segments, cfg)
        for m in mutes:
            plan.add_mute(m["start"], m["end"], m["reason"])
        print(f"  [消音] {len(mutes)} 段敏感业务数据")

    # ---- 5) 长停顿 / 口头禅 -> delete 或 speed -------------------------
    if segments and cfg.pause_mode != "off":
        pause_items, speed_items = _build_pause_plan(
            segments, plan.duration, cfg)
        for p in pause_items:
            plan.add_delete(p["start"], p["end"], p["type"], p["reason"])
        plan.speed_segments.extend(speed_items)
        print(f"  [停顿] 删除 {len(pause_items)} 段 / 变速 {len(speed_items)} 段")

    if segments and cfg.remove_filler:
        fillers = _filler_segments(segments)
        for f in fillers:
            plan.add_delete(f["start"], f["end"], T.T_FILLER, f["reason"])
        if fillers:
            print(f"  [口头禅] 删除 {len(fillers)} 句纯口头禅")

    # ---- 6) 开场裁剪（少量抽帧，结果缓存）------------------------------
    if cfg.trim_intro:
        plan.intro_trim = _cached_intro(video, cfg)
        if plan.intro_trim > 0.05:
            print(f"  [开场] 裁掉前 {plan.intro_trim:.2f}s")

    # ---- 7) 高风险画面（ASR 关键词 + 关键帧确认 + 边界扩展）-----------
    if cfg.detect_sensitive_screen:
        risk = _detect_risk_screen(video, segments, llm, cfg)
        for d in risk.get("delete", []):
            plan.add_delete(d["start"], d["end"], T.T_HIGH_RISK,
                            d.get("reason", "高风险界面"))
        for r in risk.get("review", []):
            plan.add_review(r["start"], r["end"], T.T_HIGH_RISK,
                            r.get("reason", "疑似高风险界面"))
        print(f"  [高风险画面] 删除 {len(risk.get('delete', []))} 段 / "
              f"待确认 {len(risk.get('review', []))} 段")

    # ---- 8) 字幕 cue（原始时间轴 + 脱敏文本）---------------------------
    if plan.existing_subtitle:
        # 原视频已含字幕：不生成新的字幕（避免与已有字幕重复叠加）
        plan.subtitle_cues = []
    else:
        plan.subtitle_cues = _build_cues(segments, plan.width, plan.height, cfg)

    # ---- 9) 封面主题（LLM 读全文，结果缓存）----------------------------
    plan.cover = _build_cover_info(segments, plan, llm, cfg, video)

    # ---- 10) 规范化 + 统计 + 落盘 --------------------------------------
    plan.normalize()
    plan.stats = _build_stats(plan, segments)
    path = plan_path or os.path.join(cfg.workdir, "plan.json")
    plan.save(path)
    print(f"[分析] 剪辑时间轴 -> {path}")
    print(f"[分析] 原始 {plan.duration:.1f}s -> 成片 "
          f"{plan.output_duration():.1f}s "
          f"(保留 {plan.output_duration() / max(plan.duration, 1e-6) * 100:.0f}%)")
    return plan


# ---------------------------------------------------------------------------
# 各子步骤
# ---------------------------------------------------------------------------
def _cfg_sig(cfg: Config, keys: List[str]) -> Dict[str, Any]:
    return {k: getattr(cfg, k, None) for k in keys}


def _apply_llm_sensitive(segments, llm, cfg, video) -> List[Dict[str, Any]]:
    """LLM 语义补充敏感短语（结果缓存，避免重复调用）。"""
    if not (llm and llm.available()):
        return segments
    key = cache.signature(video, {
        "kind": "llm_sensitive", "model": cfg.llm_model,
        "asr": cfg.asr_model, "n": len(segments),
    })
    hits = cache.get_or_create(
        cfg, key, "llm_sensitive",
        lambda: llm.detect_sensitive(segments) or [],
        label="LLM 敏感短语")
    if hits:
        segments = sensitive.add_llm_spans(segments, hits)
    return segments


def _cached_bargaining(segments, llm, cfg, video) -> List[Dict[str, Any]]:
    key = cache.signature(video, {
        "kind": "bargaining", "asr": cfg.asr_model,
        "llm": cfg.llm_model if (llm and llm.available()) else "none",
        **_cfg_sig(cfg, ["bargain_pad", "bargain_gap"]),
    })
    return cache.get_or_create(
        cfg, key, "bargaining",
        lambda: bargaining.detect(segments, llm, cfg),
        label="议价分析") or []


def _cached_intro(video, cfg) -> float:
    key = cache.signature(video, {
        "kind": "intro",
        **_cfg_sig(cfg, ["intro_blue_threshold", "intro_max_scan",
                         "intro_min_seconds", "intro_max_seconds",
                         "intro_meeting_step", "intro_meeting_scan"]),
    })

    def _run():
        try:
            val = intro_mod.detect_intro(
                video,
                max_scan=cfg.intro_max_scan,
                blue_thr=cfg.intro_blue_threshold,
                min_intro=cfg.intro_min_seconds,
                intro_max_seconds=cfg.intro_max_seconds,
                meeting_step=cfg.intro_meeting_step,
                meeting_scan=cfg.intro_meeting_scan,
            )
            val = float(val or 0.0)
            # 防误删：单段开场上限（detect_intro 内部已按开场窗口收敛）
            return min(val, float(cfg.intro_max_seconds))
        except Exception as e:  # noqa
            print(f"  [warn] 开场检测失败，跳过: {e}")
            return 0.0

    return float(cache.get_or_create(cfg, key, "intro", _run,
                                     label="开场检测") or 0.0)


def _detect_risk_screen(video, segments, llm, cfg) -> Dict[str, List[Dict]]:
    """高风险画面检测（ASR 关键词定位 + 边界扩展，见 risk_screen.py）。"""
    try:
        from . import risk_screen
    except ImportError:
        print("  [高风险画面] 模块未启用，跳过")
        return {"delete": [], "review": []}

    key = cache.signature(video, {
        "kind": "risk_screen", "asr": cfg.asr_model,
        "llm": cfg.llm_model if (llm and llm.available()) else "none",
        # 检测逻辑版本（2026-08-21 稳定性重构 v3：全片低帧率 OCR 扫描 +
        # 高分辨率左栏 OCR + 产品页"无强证据才压制"排除 + 2强词达删除级
        # + _expand_runs 不再二次排除 product 帧）；
        # v7（2026-08-21）：腾讯会议否决收紧为"仅 腾讯会议 产品名 + 会议号(精确)"，
        # 且仅当 n_strong==0 才否决——快速会议/预定会议/加入会议 等是**企业微信自己的
        # 会议按钮**（实测真企微帧 FULL_OCR 含"快速会议"），放进否决会把真企微帧
        # 误判成腾讯会议 → 整段漏删。改规则须 +1 失效旧缓存
        "wechat_detect_v": 7,
        **_cfg_sig(cfg, ["sensitive_screen_mode", "sensitive_screen_pad",
                         "sensitive_screen_conf_thr", "risk_screen_keyword_pad",
                         "risk_screen_sample_step", "risk_screen_max_expand",
                         "risk_screen_min_screen"]),
    })

    def _run():
        try:
            return risk_screen.detect(video, segments, llm, cfg)
        except Exception as e:  # noqa
            print(f"  [warn] 高风险画面检测失败（安全起见不删除）: {e}")
            return {"delete": [], "review": []}

    return cache.get_or_create(cfg, key, "risk_screen", _run,
                               label="高风险画面检测") or \
        {"delete": [], "review": []}


# ---- 客户问答识别 ---------------------------------------------------------
def _is_question(text: str) -> bool:
    if not text:
        return False
    if any(ch in text for ch in QUESTION_MARKS):
        return True
    return any(w in text for w in QUESTION_WORDS)


def _mark_customer_turns(segments: List[Dict[str, Any]]) -> None:
    """标记疑似客户发言 / 提问句，并把其邻域标为受保护。

    说明：未做说话人分离（diarization），这里用「提问句式 + 客户自述场景」
    的启发式判定。目的只是**避免过度剪辑**（受保护段内停顿裁剪更保守），
    不用于删除决策，因此误判代价可控。
    """
    for s in segments:
        text = s.get("text", "")
        q = _is_question(text)
        ctx = any(w in text for w in CUSTOMER_CONTEXT)
        s["is_question"] = q
        s["customer_turn"] = q or ctx

    # 客户提问后紧跟的销售回答同样属于高价值内容 -> 一并保护
    n = len(segments)
    for i, s in enumerate(segments):
        if not s.get("customer_turn"):
            continue
        s["protected"] = True
        for j in range(i + 1, min(n, i + 4)):     # 回答通常在随后 1~3 句
            segments[j]["protected"] = True
    for s in segments:
        s.setdefault("protected", False)


# ---- 敏感业务数据 -> 消音区间 --------------------------------------------
def _build_mute_segments(segments: List[Dict[str, Any]],
                         cfg: Config) -> List[Dict[str, Any]]:
    """把命中敏感短语的**词级时间范围**转成消音区间。

    关键点（需求「三、局部消音」）：不要为了隐藏一句话而删除整段客户回答。
    有词级时间轴时按词精确定位；没有时退化为整句消音。
    """
    out: List[Dict[str, Any]] = []
    for seg in segments:
        spans = seg.get("sensitive_spans") or []
        if not spans:
            continue
        words = seg.get("words") or []
        for s0, e0, matched in spans:
            rng = _char_span_to_time(seg, words, s0, e0)
            if rng is None:
                rng = (seg["start"], seg["end"])
            out.append({
                "start": max(0.0, rng[0] - 0.12),      # 略微提前，避免漏字头
                "end": rng[1] + 0.18,
                # 原因里对数字做掩码：审核清单/plan.json 也不落敏感数值
                "reason": f"涉及企业经营数据（{sensitive.mask_digits(matched)}）",
            })
    return _merge_mutes(out)


def _char_span_to_time(seg, words, c0: int, c1: int
                       ) -> Optional[Tuple[float, float]]:
    """把 text 中的字符区间映射到词级时间区间。"""
    if not words:
        return None
    # 依次累计每个词在 text 中的位置
    text = seg.get("text", "")
    cursor = 0
    hits: List[Tuple[float, float]] = []
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        pos = text.find(token, cursor)
        if pos < 0:
            pos = cursor
        end = pos + len(token)
        cursor = end
        if end > c0 and pos < c1:                  # 与敏感字符区间相交
            hits.append((float(w["start"]), float(w["end"])))
    if not hits:
        return None
    return (min(h[0] for h in hits), max(h[1] for h in hits))


def _merge_mutes(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return []
    items = sorted(items, key=lambda x: x["start"])
    out = [dict(items[0])]
    for it in items[1:]:
        if it["start"] <= out[-1]["end"] + 0.25:
            out[-1]["end"] = max(out[-1]["end"], it["end"])
            if it["reason"] not in out[-1]["reason"]:
                out[-1]["reason"] += "；" + it["reason"]
        else:
            out.append(dict(it))
    return out


# ---- 停顿处理（不使用 AI）-------------------------------------------------
def _build_pause_plan(segments: List[Dict[str, Any]], duration: float,
                      cfg: Config) -> Tuple[List[Dict], List[Dict]]:
    """基于 ASR 句间隔生成停顿删除/变速区间。

    trim 模式（默认）：
      gap <= 1s            保持
      1s < gap <= 3s       裁短到 pause_trim_to
      gap > 3s             删除，仅保留 pause_delete_keep 作为过渡
    受保护（客户问答）区域内，保留时长放宽到 customer_pause_keep，
    避免把客户问题剪碎。
    """
    deletes: List[Dict[str, Any]] = []
    speeds: List[Dict[str, Any]] = []
    mode = (cfg.pause_mode or "trim").lower()

    bounds: List[Tuple[float, float, bool]] = []
    prev_end = 0.0
    prev_protected = False
    for seg in segments:
        gap = seg["start"] - prev_end
        if gap > cfg.pause_keep_threshold:
            protected = prev_protected or bool(seg.get("protected"))
            bounds.append((prev_end, seg["start"], protected))
        prev_end = seg["end"]
        prev_protected = bool(seg.get("protected"))
    # 片尾静音（留 0.4s 收尾，避免最后一帧硬切/无声）
    if duration - prev_end > cfg.pause_keep_threshold:
        bounds.append((prev_end, max(prev_end, duration - 0.4), False))

    for s, e, protected in bounds:
        gap = e - s
        if gap <= cfg.pause_keep_threshold:
            continue

        if gap > cfg.pause_speed_threshold:
            keep = cfg.pause_delete_keep
            if protected and cfg.protect_customer_qa:
                keep = max(keep, cfg.customer_pause_keep)
            cut_s = s + keep / 2
            cut_e = e - keep / 2
            if cut_e - cut_s > 0.08:
                deletes.append({
                    "start": cut_s, "end": cut_e, "type": T.T_LONG_PAUSE,
                    "reason": f"长停顿 {gap:.1f}s（保留 {keep:.2f}s 过渡）",
                })
            continue

        # 1~3 秒停顿
        if mode == "speed":
            speeds.append({"start": s, "end": e,
                           "speed": float(cfg.pause_speed_factor),
                           "type": T.T_LONG_PAUSE})
            continue
        keep = cfg.pause_trim_to
        if protected and cfg.protect_customer_qa:
            keep = max(keep, cfg.customer_pause_keep, cfg.pause_trim_to)
        if gap - keep > 0.12:
            cut_s = s + keep / 2
            cut_e = e - (keep / 2)
            deletes.append({
                "start": cut_s, "end": cut_e, "type": T.T_LONG_PAUSE,
                "reason": f"停顿 {gap:.1f}s 裁短至 {keep:.2f}s",
            })
    return deletes, speeds


def _norm_filler(text: str) -> str:
    return _PUNCT_RE.sub("", text or "")


def _filler_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """仅当整句去掉标点后完全由口头禅构成时才删除（保守策略）。"""
    out = []
    for seg in segments:
        if seg.get("protected"):
            continue                     # 客户问答区不动
        raw = _norm_filler(seg.get("text", ""))
        if not raw or len(raw) > 8:
            continue
        rest = raw
        for tok in sorted(FILLER_TOKENS, key=len, reverse=True):
            rest = rest.replace(tok, "")
        if rest == "":
            out.append({"start": seg["start"], "end": seg["end"],
                        "reason": f"无意义口头禅「{seg.get('text', '').strip()}」"})
    return out


# ---- 字幕 / 封面 / 统计 ---------------------------------------------------
_SENT_BREAKS = "。！？；…?!;"


def _max_subtitle_chars(width: int, fontsize: int) -> int:
    """单行字幕最多容纳的中文字符数（含 margin，保证绝不出两行）。

    ASS 字幕在宽 width 的画面上可用宽度 ≈ width*(1-2*0.05)（subtitle.py 的
    margin_lr），每个中文字符宽度 ≈ fontsize。为保险再乘 0.95 系数，
    并夹在 [10, 40] 之间（太短会碎片化，太长观众读不完）。
    """
    usable = int(width * (1 - 2 * 0.05) * 0.95)
    n = max(1, usable // max(fontsize, 1))
    return max(10, min(n, 40))


def _split_cue(seg: Dict[str, Any], max_chars: int) -> List[Dict[str, Any]]:
    """把一条 ASR 段拆成 ≤ max_chars 字的**单行**字幕 cue 列表。

    用户诉求：字幕别老是出现两行。whisper 单段常 20~60 字，直接做一条 cue
    会被 libass 按 WrapStyle 自动折成两行 → 必须按字符数拆分。

    时间分配：
      - 有词级时间戳（seg["words"]）→ 按「字符在整段中的位置」映射到词时间，
        精确且与语音逐词对齐；
      - 无词级时间戳 → 按字符数比例均分整段时间（可接受的近似）。
    断句：优先在句末标点后断行（.！？…），行长度 < max_chars//2 时不断。
    """
    text = seg.get("text") or ""
    redacted = seg.get("redacted_text") or text
    n = len(text)
    if n <= max_chars or n <= 1:
        return [{"start": float(seg["start"]), "end": float(seg["end"]),
                 "text": redacted.strip(),
                 "redacted": bool(seg.get("sensitive_spans"))}]

    # ---- 计算切分点（字符区间）----
    cuts: List[Tuple[int, int]] = []
    i = 0
    while i < n:
        j = min(i + max_chars, n)
        # 在 [i+max_chars//2, j) 内找最后一个句末标点，从标点后断行
        for k in range(j - 1, i + max_chars // 2 - 1, -1):
            if text[k] in _SENT_BREAKS:
                j = k + 1
                break
        cuts.append((i, j))
        i = j

    seg_start, seg_end = float(seg["start"]), float(seg["end"])
    words = seg.get("words") or []

    # ---- 字符位置 → 时间（有 words 时精确映射）----
    char_times: Dict[int, Tuple[float, float]] = {}
    if words:
        cursor = 0
        for w in words:
            token = (w.get("word") or "").strip()
            if not token:
                continue
            pos = text.find(token, cursor)
            if pos < 0:
                pos = cursor
            endp = pos + len(token)
            cursor = endp
            char_times[endp] = (float(w["start"]), float(w["end"]))
            char_times.setdefault(pos, (float(w["start"]), float(w["end"])))

    def _char_to_time(c0: int, c1: int) -> Tuple[float, float]:
        if char_times:
            hits = [ts for cp, ts in char_times.items() if c0 <= cp < c1]
            if hits:
                s0, e0 = min(h[0] for h in hits), max(h[1] for h in hits)
                if c0 == 0:
                    s0 = seg_start               # 首片起点 = 段起点
                # 词时间戳夹取在段范围内（防词时间越界把片长顶出段尾）
                s0 = max(seg_start, min(s0, seg_end))
                e0 = max(seg_start, min(e0, seg_end))
                return (s0, e0)
            # 该区间无词边界：回退比例法
        frac = (c0 + c1) / 2.0 / max(n, 1)
        mid = seg_start + (seg_end - seg_start) * frac
        s = max(seg_start, mid - 0.35)
        e = min(seg_end, mid + 0.35)
        if c0 == 0:
            s = seg_start
        return (s, e)

    out: List[Dict[str, Any]] = []
    for idx, (c0, c1) in enumerate(cuts):
        s, e = _char_to_time(c0, c1)
        # 避免下一条 start 早于上一条 end（相邻词时间戳可能重叠/倒序）
        if out and s < out[-1]["end"]:
            s = out[-1]["end"]
        # 末片延伸到段尾，避免文本结束后残留时间被丢掉（字幕提前消失）
        if idx == len(cuts) - 1:
            e = seg_end
        piece = redacted[c0:c1].strip()
        if not piece:
            continue
        out.append({"start": round(s, 3), "end": round(max(e, s + 0.1), 3),
                    "text": piece,
                    "redacted": bool(seg.get("sensitive_spans"))})
    return out or [{"start": seg_start, "end": seg_end, "text": redacted.strip(),
                    "redacted": bool(seg.get("sensitive_spans"))}]


def _build_cues(segments: List[Dict[str, Any]],
                width: int = 1920, height: int = 1080,
                cfg: Optional[Config] = None) -> List[Dict[str, Any]]:
    """字幕 cue：一律使用脱敏文本，原始敏感数据绝不进入字幕。

    2026-08-21 增强：按单行容量拆分长句（_split_cue），保证字幕**单行**，
    不再被 ASS 自动折成两行；每条 cue 时间由词级时间戳精确分配。
    """
    max_chars = _max_subtitle_chars(width,
                                    int(getattr(cfg, "subtitle_fontsize", 48) or 48))
    cues: List[Dict[str, Any]] = []
    for seg in segments:
        cues.extend(_split_cue(seg, max_chars))
    return cues


def _build_cover_info(segments, plan: EditPlan, llm, cfg,
                      video: str) -> Dict[str, Any]:
    """封面：从正片挑选「产品页面」帧为底，AI 从全文提炼短主题。

    不再用成片首帧（首帧常为白底等候室/蓝屏，不适合做封面），
    而是用 pick_product_page_frame 在开场之后的正片里挑一帧最像软件
    产品界面的画面（内容多、有色彩、非白底、非蓝屏），并跳过已删除
    的敏感段(如企业微信)。
    """
    from .utils import get_duration
    after_ts = plan.intro_trim + 0.5 if plan.intro_trim > 0.05 else 0.0
    dur = get_duration(video)
    # 跳过已删除段(企业微信等)，避免把敏感画面当封面
    exclude = [(float(d["start"]), float(d["end"])) for d in plan.delete_segments]
    frame_ts = intro_mod.pick_product_page_frame(
        video, after_ts, dur,
        n_samples=getattr(cfg, "cover_frame_samples", 16),
        exclude_ranges=exclude)
    title = ""
    # 用户显式指定封面标题时优先使用（覆盖 LLM 提炼）
    if getattr(cfg, "cover_title", ""):
        title = cfg.cover_title
    else:
        if llm and llm.available() and segments:
            transcript = " ".join(s.get("text", "") for s in segments[:200])
            key = cache.signature(video, {"kind": "cover_title",
                                          "model": cfg.llm_model,
                                          "asr": cfg.asr_model})
            title = cache.get_or_create(
                cfg, key, "cover_title",
                lambda: llm.cover_title(transcript) or "",
                label="封面标题") or ""
        # 说明：封面标题优先级见上方显式 cover_title > 本分支 LLM。经智能体（WorkBuddy/Codex）
        # 使用时，智能体用自身 LLM 生成标题并通过 --cover-title 注入 cfg.cover_title，不会走到这里。
        # 本分支是「纯命令行无智能体」的后备：仅当配置了外部 AVEditor_LLM_* 时才调用 LLM；
        # 否则标题留空并提示，交由人工在视频号草稿标题处补充（坚持高质量，不做首句兜底）。
        if not title and not (llm and llm.available()):
            print("[warn] 未配置 LLM_API_KEY，跳过自动标题生成。"
                  "请在环境变量设置 AVEditor_LLM_API_KEY / AVEditor_LLM_BASE_URL"
                  " / AVEditor_LLM_MODEL 后重试，或在 plan.json 显式指定 cover_title。")
    return {"title": title, "frame_ts": float(frame_ts),
            "duration": float(cfg.cover_duration)}


def _build_stats(plan: EditPlan, segments) -> Dict[str, Any]:
    by_type: Dict[str, float] = {}
    for d in plan.delete_segments:
        t = d.get("type", "?")
        by_type[t] = by_type.get(t, 0.0) + (d["end"] - d["start"])
    if plan.intro_trim > 1e-3:
        by_type[T.T_INTRO] = plan.intro_trim
    out_dur = plan.output_duration()
    return {
        "asr_segments": len(segments),
        "customer_turns": sum(1 for s in segments if s.get("customer_turn")),
        "protected_segments": sum(1 for s in segments if s.get("protected")),
        "deleted_seconds_by_type": {k: round(v, 2) for k, v in by_type.items()},
        "deleted_seconds_total": round(plan.duration - out_dur, 2),
        "output_duration": round(out_dur, 2),
        "keep_ratio": round(out_dur / max(plan.duration, 1e-6), 4),
        "mute_count": len(plan.mute_segments),
        "review_count": len(plan.review_items),
    }


def write_review_report(plan: EditPlan, cfg: Config,
                        path: Optional[str] = None) -> str:
    path = path or os.path.join(cfg.workdir, "审核清单.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(plan.review_report())
    return path
